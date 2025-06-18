import asyncio
import base64
import io
import os
import re
from typing import Any, Coroutine

import aiohttp
from PIL.ImageFile import ImageFile
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig
from PIL import Image
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
        self.session = None  # 初始化session

    @filter.command("xyzw", "识别宝箱")
    async def start_command(self, event: AstrMessageEvent):
        """命令触发：开始识别流程"""
        user_id = event.get_sender_id()
        # 设置该用户为等待图片状态
        self.waiting_for_image[user_id] = True
        # 回复用户，要求发送图片
        yield event.plain_result("🖼🖼🖼️ 请发送宝箱截图（60秒内）")

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
        logger.info(f"用户 {user_id} 发送了图片")
        logger.info(message_chain)
        image_url = None
        for msg in message_chain:
            if hasattr(msg, 'type') and msg.type == 'Image':
                try:
                    if msg.url:  # 普通URL图片
                        image_url = msg.url
                    elif msg.file:  # Base64图片
                        # 直接保存为临时文件
                        image_path = await self.save_base64_image(msg.file)
                    break
                except Exception as e:
                    logger.error(f"图片处理失败: {str(e)}")
                    yield event.plain_result("❌ 图片解析失败，请重新发送")
                    return

        if not image_url:
            # 如果没有图片，提示用户
            logger.error("没有找到图片，请重新发送")
            return

        # 清除等待状态
        del self.waiting_for_image[user_id]

        try:
            # 下载图片
            yield event.plain_result("🔍🔍 开始处理图片...")
            image_path = await self.download_image(image_url)

            # 处理图片并获取结果
            result = await self.process_image(image_path)

            # 发送结果
            yield event.plain_result(f"✅ 识别完成\r{result}")

        except Exception as e:
            logger.error(f"处理失败: {str(e)}")
            yield event.plain_result(f"❌❌ 处理失败: {str(e)}")

    async def save_base64_image(self, base64_str: str) -> str:
        """将base64保存为临时文件并返回路径"""
        if base64_str.startswith("base64://"):
            base64_str = base64_str[len("base64://"):]

        try:
            image_data = base64.b64decode(base64_str)
            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp_file:
                tmp_file.write(image_data)
                return tmp_file.name
        except Exception as e:
            logger.error(f"Base64保存失败: {str(e)}")
            raise Exception("图片保存失败")


    # 转换base64为 图片
    async def convert_base64_to_image(self, base64_str: str) -> Image.Image:
        # 处理微信的特殊前缀
        if base64_str.startswith("base64://"):
            base64_str = base64_str[len("base64://"):]  # 移除前缀

        # 处理其他可能的 base64 前缀格式（如 data:image/jpeg;base64,）
        elif base64_str.startswith("data:image/") and ";base64," in base64_str:
            base64_str = base64_str.split(",", 1)[1]

        try:
            image_data = base64.b64decode(base64_str)
            return Image.open(io.BytesIO(image_data))
        except Exception as e:
            logger.error(f"Base64 解码失败: {str(e)}")
            raise Exception("图片格式无效，请发送标准图片")

    async def download_image(self, url: str) -> str:
        """异步下载图片到本地临时文件"""
        if not self.session:
            self.session = aiohttp.ClientSession()

        try:
            async with self.session.get(url) as response:
                if response.status != 200:
                    raise Exception(f"下载图片失败: HTTP {response.status}")

                # 创建临时文件
                _, ext = os.path.splitext(url)
                with tempfile.NamedTemporaryFile(suffix=ext or ".jpg", delete=False) as tmp_file:
                    while True:
                        chunk = await response.content.read(8192)
                        if not chunk:
                            break
                        tmp_file.write(chunk)
                    return tmp_file.name
        except Exception as e:
            logger.error(f"图片下载失败: {str(e)}")
            raise Exception("图片下载失败，请重试")

    async def process_image(self, image_path: str) -> str:
        """处理图片并返回结果"""
        cut1_path, cut2_path = None, None
        try:
            # 1. 裁剪图片
            cut1_path, cut2_path = self.crop_image(image_path)

            # 2. 异步并发执行OCR识别
            cut1_text, cut2_text = await asyncio.gather(
                self.async_ocr_text(cut1_path),
                self.async_ocr_text(cut2_path)
            )

            # 3. 数据解析
            pre_code = self.parse_pre_code(cut1_text)
            wooden, silver, gold, platinum = self.parse_materials(cut2_text)

            # 4. 计算积分
            return self.calculate_result(wooden, silver, gold, platinum, pre_code)

        finally:
            # 清理临时文件
            for path in [image_path, cut1_path, cut2_path]:
                if path and os.path.exists(path):
                    os.unlink(path)

    async def async_ocr_text(self, image_path: str) -> str:
        """异步OCR识别文本"""
        logger.info(f"使用异步OCR处理图片: {image_path}")

        if not self.session:
            self.session = aiohttp.ClientSession()

        url = f"{self.ocr_url}/parse/image"
        data = aiohttp.FormData()
        data.add_field('apikey', self.ocr_key)
        data.add_field('language', 'chs')
        data.add_field('OCREngine', '2')
        data.add_field('file', open(image_path, 'rb'), filename=os.path.basename(image_path))

        try:
            async with self.session.post(url, data=data) as response:
                if response.status != 200:
                    error_msg = await response.text()
                    logger.error(f"OCR API错误: {error_msg}")
                    raise Exception(f"OCR服务错误: HTTP {response.status}")

                response_data = await response.json()
                return response_data["ParsedResults"][0]["ParsedText"]

        except (KeyError, IndexError) as e:
            logger.error(f"解析OCR响应失败: {str(e)}")
            raise Exception("OCR响应解析失败")
        except json.JSONDecodeError:
            logger.error("无效的OCR响应")
            raise Exception("OCR服务返回了无效的响应")
        except Exception as e:
            logger.error(f"OCR请求失败: {str(e)}")
            raise Exception("OCR服务请求失败")

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