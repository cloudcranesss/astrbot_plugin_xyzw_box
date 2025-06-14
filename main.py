import asyncio
import os
import re
import aiohttp
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig
from PIL import Image
import requests
import tempfile
import json

from astrbot.core.star.filter.event_message_type import EventMessageType


@register("咸鱼之王-宝箱识别", "cloudcranesss", "通过OCR识别咸鱼之王游戏中的宝箱数量", "1.0.1")
class BaoXiangPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config or {}
        self.waiting_for_image = {}
        self.ocr_url = self.config.get("ocr_url", "")
        self.ocr_key = self.config.get("ocr_api_key", "")
        logger.info(f"ocr_url {self.ocr_url} ocr_key: {self.ocr_key}")
        logger.info("宝箱识别插件已初始化")

    @filter.command("xyzw", "识别宝箱")
    async def start_command(self, event: AstrMessageEvent):
        """命令触发：开始识别流程"""
        user_id = event.get_sender_id()
        # 设置该用户为等待图片状态
        self.waiting_for_image[user_id] = True
        # 回复用户，要求发送图片
        yield event.plain_result("🖼️ 请发送宝箱截图（60秒内）")

        # 设置一个定时器，60秒后清除等待状态
        async def clear_state():
            await asyncio.sleep(60)
            if user_id in self.waiting_for_image:
                del self.waiting_for_image[user_id]
                logger.error("图片识别超时，已取消等待")

        asyncio.create_task(clear_state())

    @filter.event_message_type(EventMessageType.ALL)
    async def handle_image(self, event: AstrMessageEvent):
        """处理用户发送的图片（如果处于等待状态）"""
        user_id = event.get_sender_id()
        # 如果用户不在等待状态，则忽略
        if user_id not in self.waiting_for_image:
            return

        message_chain = event.get_messages()
        image_url = None
        for msg in message_chain:
            if hasattr(msg, 'type') and msg.type == 'Image':
                image_url = msg.url
                break

        if not image_url:
            # 如果没有图片，提示用户
            logger.error("没有找到图片，请重新发送")
            return

        # 清除等待状态
        del self.waiting_for_image[user_id]

        try:
            # 下载图片
            yield event.plain_result("🔍 开始处理图片...")
            image_path = await self.download_image(image_url)

            # 处理图片并获取结果
            result = await self.process_image(image_path)

            # 发送结果
            yield event.plain_result(f"✅ 识别完成\r{result}")

        except Exception as e:
            logger.error(f"处理失败: {str(e)}")
            yield event.plain_result(f"❌ 处理失败: {str(e)}")

    async def download_image(self, url: str) -> str:
        """下载图片到本地临时文件"""
        try:
            response = requests.get(url, stream=True)
            if response.status_code != 200:
                raise Exception(f"下载图片失败: HTTP {response.status_code}")

            # 创建临时文件
            _, ext = os.path.splitext(url)
            with tempfile.NamedTemporaryFile(suffix=ext or ".jpg", delete=False) as tmp_file:
                for chunk in response.iter_content(chunk_size=8192):
                    tmp_file.write(chunk)
                return tmp_file.name

        except Exception as e:
            logger.error(f"图片下载失败: {str(e)}")
            raise Exception("图片下载失败，请重试")

    async def init_session(self):
        """初始化异步会话"""
        try:
            if not hasattr(self, 'session') or not self.session or self.session.closed:
                self.session = aiohttp.ClientSession()
        except Exception as e:
            logger.error(f"会话初始化失败: {str(e)}")
            raise

    async def process_image(self, image_path: str) -> str:
        """处理图片并返回结果"""
        cut1_path, cut2_path = None, None
        try:
            # 初始化会话
            await self.init_session()

            # 1. 裁剪图片
            cut1_path, cut2_path = self.crop_image(image_path)

            # 2. OCR识别
            cut1_text = self.ocr_text(cut1_path)
            cut2_text = self.ocr_text(cut2_path)

            # 3. 数据解析
            pre_code = self.parse_pre_code(cut1_text)
            wooden, silver, gold, platinum = self.parse_materials(cut2_text)

            # 4. 计算积分
            return self.calculate_result(wooden, silver, gold, platinum, pre_code)

        finally:
            # 清理临时文件和关闭会话
            if image_path and os.path.exists(image_path):
                os.unlink(image_path)
            if cut1_path and os.path.exists(cut1_path):
                os.unlink(cut1_path)
            if cut2_path and os.path.exists(cut2_path):
                os.unlink(cut2_path)
            await self.close_session()

    async def close_session(self):
        """关闭异步会话"""
        try:
            if hasattr(self, 'session') and self.session and not self.session.closed:
                await self.session.close()
        except Exception as e:
            logger.error(f"会话关闭失败: {str(e)}")
        finally:
            self.session = None

    def crop_image(self, image_path: str) -> tuple[str, str]:
        """裁剪图片并返回路径"""
        try:
            img = Image.open(image_path)
            width, height = img.size

            # 顶部区域（预设积分）
            box_top = (0, int(height * 0.15), int(width * 0.5), int(height * 0.3))
            # 底部区域（宝箱数量）
            box_bottom = (0, int(height * 0.75), width, int(height * 0.87))

            # 创建临时文件
            dir_path = os.path.dirname(image_path)
            cut1_path = os.path.join(dir_path, "cut1.jpg")
            cut2_path = os.path.join(dir_path, "cut2.jpg")

            # 裁剪并保存
            img.crop(box_top).save(cut1_path)
            img.crop(box_bottom).save(cut2_path)

            return cut1_path, cut2_path

        except Exception as e:
            logger.error(f"图片裁剪失败: {str(e)}")
            raise Exception("图片处理失败，请确保发送的是有效的游戏截图")

    def ocr_text(self, image_path: str) -> str:
        """OCR识别文本"""
        logger.info(f"使用OCR处理图片: {image_path}")

        url = f"{self.ocr_url}/parse/image"
        files = {"file": open(image_path, "rb")}
        payload = {
            "apikey": self.ocr_key,
            "language": "chs",
            "OCREngine": "2"
        }
        response = requests.post(url, files=files, data=payload)

        if response.status_code != 200:
            # 尝试解析错误信息
            try:
                error_data = response.json()
                error_msg = error_data.get("ErrorMessage", response.text)
            except:
                error_msg = response.text
            logger.error(f"OCR API错误: {error_msg}")
            raise Exception(f"OCR服务错误: {error_msg}")

        # 解析响应
        try:
            response_data = response.json()
            return response_data["ParsedResults"][0]["ParsedText"]
        except (KeyError, IndexError, TypeError) as e:
            logger.error(f"解析OCR响应失败: {str(e)}, 响应内容: {response.text[:200]}")
            raise Exception("OCR响应解析失败")
        except json.JSONDecodeError:
            logger.error(f"无效的OCR响应: {response.text[:200]}")
            raise Exception("OCR服务返回了无效的响应")

    def parse_pre_code(self, text: str) -> int:
        """解析预设积分"""
        match = re.search(r"\d+", text)
        if not match:
            raise ValueError("无法解析预设积分")
        return int(match.group())

    def parse_materials(self, text: str) -> tuple[int, int, int, int]:
        """解析四种宝箱数量"""
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if len(lines) < 4:
            raise ValueError(f"OCR结果行数不足: {text}")

        cleaned = [
            line.replace("o", "0").replace("O", "0")
            .replace("l", "1").replace("L", "1")
            .replace("I", "1").replace("i", "1")
            .replace("|", "1").replace("!", "1")
            for line in lines[:4]
        ]

        # 仅保留数字字符
        cleaned = [re.sub(r"[^\d]", "", line) for line in cleaned]

        # 确保每个值都有有效数字
        if any(not line for line in cleaned):
            raise ValueError(f"OCR结果包含无效数字: {cleaned}")

        return (
            int(cleaned[0]), int(cleaned[1]),
            int(cleaned[2]), int(cleaned[3])
        )

    def calculate_result(self, wooden: int, silver: int, gold: int, platinum: int, pre_code: int) -> str:
        """计算并返回结果字符串"""
        total = wooden + silver * 10 + gold * 20 + platinum * 50
        NEED_CODE = 3340  # 一轮所需积分
        adjusted_code = self.adjust_pre_code(pre_code)

        if total >= adjusted_code:
            remaining = total - adjusted_code
            rounds = remaining // NEED_CODE
            surplus = NEED_CODE - (remaining % NEED_CODE)
            rounds += 1  # 包含已完成的预设轮
        else:
            surplus = adjusted_code - total
            rounds = 0

        return (
            f"📦 木头箱: {wooden}\n"
            f"🥈 白银箱: {silver}\n"
            f"🥇 黄金箱: {gold}\n"
            f"💎 铂金箱: {platinum}\n"
            f"🔄 可完成轮数: {rounds}\n"
            f"🎯 当前积分: {total}\n"
            f"🚧 下一轮还需: {surplus}\n"
            f"⚔️ 推荐闯关数: {surplus / 2.5:.1f}"
        )

    def adjust_pre_code(self, pre_code: int) -> int:
        """调整预设积分逻辑"""
        if pre_code >= 6000:
            return 860 - (pre_code - 6000) // 25 * 12
        elif pre_code >= 4000:
            return 1720 - (pre_code - 4000) // 25 * 12
        elif pre_code >= 2000:
            return 2580 - (pre_code - 2000) // 25 * 12
        elif pre_code >= 1000:
            return 480 - (pre_code - 1000) // 25 * 12 + 2580
        else:
            return 3440