import asyncio
import base64
import binascii
import os
import re
import tempfile
import json
import uuid
from typing import Dict, Optional, Any, Coroutine
import aiofiles
import aiohttp
from PIL import Image
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig
from astrbot.core.star.filter.event_message_type import EventMessageType


@register("咸鱼之王-宝箱识别", "cloudcranesss", "通过OCR识别咸鱼之王游戏中的宝箱数量", "1.0.2")
class BaoXiangPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config or {}
        self.waiting_for_image: Dict[str, bool] = {}  # 用户ID: 是否在等待图片
        self.timeout_tasks: Dict[str, asyncio.Task] = {}  # 用户ID: 超时任务
        self.ocr_url = self.config.get("ocr_url", "")
        self.ocr_key = self.config.get("ocr_api_key", "")
        logger.info(f"ocr_url {self.ocr_url} ocr_key: {self.ocr_key}")
        logger.info("宝箱识别插件已初始化")
        self.session: Optional[aiohttp.ClientSession] = None

    async def terminate(self):
        """清理资源，由外部调用（例如在插件卸载时）"""
        # 取消所有超时任务
        for user_id, task in self.timeout_tasks.items():
            task.cancel()
        self.timeout_tasks.clear()
        self.waiting_for_image.clear()

        # 关闭会话
        if self.session:
            await self.session.close()
            self.session = None
        logger.info("宝箱识别插件已清理")

    @filter.command("xyzw", "识别宝箱")
    async def start_command(self, event: AstrMessageEvent):
        """命令触发：开始识别流程"""
        user_id = event.get_sender_id()

        # 检查是否已有等待中的请求
        if user_id in self.waiting_for_image:
            yield event.plain_result("⚠️ 您已有待处理的图片请求，请先发送截图或输入 'q' 退出")
            return

        # 设置该用户为等待图片状态
        self.waiting_for_image[user_id] = True
        # 回复用户，要求发送图片
        yield event.plain_result("🖼️ 请发送宝箱截图（60秒内），输入 'q' 可退出识别流程")

        # 创建超时任务
        async def timeout_task():
            await asyncio.sleep(60)
            if user_id in self.waiting_for_image:
                del self.waiting_for_image[user_id]
                if user_id in self.timeout_tasks:
                    del self.timeout_tasks[user_id]
                logger.info(f"用户 {user_id} 图片识别超时")
                # 使用上下文发送消息
                await event.send("❌ 图片识别超时，请重新发送图片")

        task = asyncio.create_task(timeout_task())
        self.timeout_tasks[user_id] = task

    @filter.event_message_type(EventMessageType.ALL)
    async def handle_image(self, event: AstrMessageEvent):
        """处理所有消息，检查是否为图片消息或退出指令"""
        user_id = event.get_sender_id()

        # 首先检查退出指令
        if user_id in self.waiting_for_image and event.get_message_outline().strip().lower() == "q":
            # 清除等待状态
            del self.waiting_for_image[user_id]
            # 取消超时任务
            if user_id in self.timeout_tasks:
                task = self.timeout_tasks[user_id]
                task.cancel()
                del self.timeout_tasks[user_id]

            yield event.plain_result("已退出识别流程")
            return

        # 检查用户是否在等待状态
        if user_id not in self.waiting_for_image:
            return

        # 检查消息中是否包含图片
        has_image = False
        for msg in event.get_messages():
            if getattr(msg, 'type', '') == 'Image':
                has_image = True
                break

        if not has_image:
            return

        # 立即清除等待状态并取消超时任务
        del self.waiting_for_image[user_id]
        if user_id in self.timeout_tasks:
            task = self.timeout_tasks[user_id]
            task.cancel()  # 取消超时任务
            del self.timeout_tasks[user_id]

        message_chain = event.get_messages()
        logger.info(f"用户 {user_id} 发送了图片消息")
        logger.info(message_chain)
        # with open(f"data/{uuid.uuid4()}.txt", "w") as f:
        #     f.write(str(message_chain))
        #     logger.info(f"文本保存成功: {f.name}")

        image_path = None
        image_url = None

        for msg in message_chain:
            if getattr(msg, 'type', '') == 'Image':
                try:
                    # 1. 优先处理URL图片
                    if hasattr(msg, 'url') and msg.url:
                        if msg.url.startswith("http"):
                            image_url = msg.url
                        else:
                            image_path = msg.url
                        break

                    # 2. 其次处理Base64图片
                    if hasattr(msg, 'file') and msg.file:
                        logger.info({msg.file})
                        image_path = await self.save_base64_image(msg.file)
                        break
                except Exception as e:
                    logger.error(f"图片处理失败: {str(e)}")
                    yield event.plain_result("❌ 图片解析失败，请重试")
                    return

        if not image_path and not image_url:
            logger.error("消息中未检测到有效图片")
            yield event.plain_result("❌ 未检测到有效图片格式，请发送标准截图")
            return

        try:
            yield event.plain_result("🔍 开始处理图片...")

            # 下载网络图片
            if image_url and not image_path:
                image_path = await self.download_image(image_url)

            # 验证图片大小 (最大5MB)
            if os.path.getsize(image_path) > 5 * 1024 * 1024:
                raise ValueError("图片过大，请发送小于5MB的截图")

            # 处理图片并获取结果
            result = await self.process_image(image_path)
            yield event.plain_result(f"✅ 识别完成\n{result}")

        except Exception as e:
            logger.error(f"处理失败: {str(e)}")
            yield event.plain_result(f"❌ 处理失败: {str(e)}")
        # finally:
        #     # 清理临时文件
        #     if image_path and os.path.exists(image_path):
        #         os.unlink(image_path)

    async def save_base64_image(self, base64_str: str) -> str:
        pattern = r"base64://"
        base64_str = re.sub(pattern, "", base64_str)
        # 进一步移除非Base64字符（只保留字母、数字、+、/、=）
        base64_str = re.sub(r'[^a-zA-Z0-9+/=]', '', base64_str)
        logger.info(f"Base64图片保存中: {base64_str}")
        logger.info({len(base64_str)})

        temp_dir = tempfile.gettempdir()
        file_name = f"wx_image_{uuid.uuid4().hex}.jpg"
        temp_path = os.path.join(temp_dir, file_name)

        os.makedirs(temp_dir, exist_ok=True)

        try:
            decoder = base64.b64decode(base64_str)
        except binascii.Error as e:
            raise ValueError(f"Base64解码失败: {str(e)}")

        CHUNK_SIZE = 4096
        async with aiofiles.open(temp_path, "wb") as f:
            for i in range(0, len(decoder), CHUNK_SIZE):
                chunk = decoder[i:i + CHUNK_SIZE]
                await f.write(chunk)

        logger.info(f"Base64图片保存成功: {temp_path}")
        return temp_path

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
                if not ext or ext not in [".jpg", ".jpeg", ".png"]:
                    ext = ".jpg"

                temp_dir = tempfile.gettempdir()
                file_name = f"download_{uuid.uuid4().hex}{ext}"
                temp_path = os.path.join(temp_dir, file_name)

                # 使用 aiofiles 异步写入
                async with aiofiles.open(temp_path, "wb") as f:
                    # 分块读取和写入，优化大文件处理
                    CHUNK_SIZE = 1024 * 1024  # 1MB 分块
                    while True:
                        chunk = await response.content.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        await f.write(chunk)

                return temp_path

        except Exception as e:
            logger.error(f"图片下载失败: {str(e)}")
            raise Exception("图片下载失败，请重试")

    async def process_image(self, image_path: str) -> str:
        """处理图片并返回结果"""
        cut1_path, cut2_path = None, None
        try:
            # 1. 裁剪图片
            cut1_path, cut2_path = await asyncio.to_thread(self.crop_image, image_path)

            # 2. 异步并发执行OCR识别
            cut1_text, cut2_text = await asyncio.gather(
                self.async_ocr_text(cut1_path),
                self.async_ocr_text(cut2_path)
            )

            # 3. 数据解析
            pre_code = await asyncio.to_thread(self.parse_pre_code, cut1_text)
            wooden, silver, gold, platinum = await asyncio.to_thread(
                self.parse_materials, cut2_text
            )

            # 4. 计算积分
            return await asyncio.to_thread(
                self.calculate_result, wooden, silver, gold, platinum, pre_code
            )

        finally:
            logger.info("图片处理完成")
            # 清理临时文件
            for path in [cut1_path, cut2_path]:
                if path and os.path.exists(path):
                    os.unlink(path)

    def crop_image(self, image_path: str) -> tuple[str, str]:
        """裁剪图片并返回路径"""
        try:
            # 允许加载截断的图片
            from PIL import ImageFile
            ImageFile.LOAD_TRUNCATED_IMAGES = True

            img = Image.open(image_path)
            img.load()  # 强制加载所有数据
            width, height = img.size

            # 顶部区域（预设积分）
            box_top = (0, int(height * 0.15), int(width * 0.5), int(height * 0.3))
            # 底部区域（宝箱数量）
            box_bottom = (0, int(height * 0.75), width, int(height * 0.87))

            # 创建临时文件
            dir_path = os.path.dirname(image_path)
            cut1_path = os.path.join(dir_path, f"cut1_{uuid.uuid4().hex}.jpg")
            cut2_path = os.path.join(dir_path, f"cut2_{uuid.uuid4().hex}.jpg")

            # 裁剪并保存
            img.crop(box_top).save(cut1_path)
            img.crop(box_bottom).save(cut2_path)

            return cut1_path, cut2_path

        except Exception as e:
            logger.error(f"图片裁剪失败: {str(e)}")
            raise Exception("图片处理失败，请确保发送的是有效的游戏截图")

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

        # 使用 aiofiles 异步读取图片文件
        async with aiofiles.open(image_path, "rb") as f:
            image_data = await f.read()

        data.add_field('file', image_data, filename=os.path.basename(image_path))

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
        except aiohttp.ClientError as e:
            logger.error(f"OCR网络请求失败: {str(e)}")
            raise Exception("OCR服务连接失败，请稍后重试")
        except Exception as e:
            logger.error(f"OCR请求失败: {str(e)}")
            raise Exception("OCR服务请求失败")

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
            f"⚔ 推荐闯关数: {surplus / 2.5:.1f}"
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