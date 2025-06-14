import re
import traceback
from io import BytesIO
import json
import asyncio
import aiohttp
from PIL import Image
from typing import Tuple, Optional
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig
from astrbot.core.star.filter.event_message_type import EventMessageType


@register("xyzw_box", "cloudcranes", "一个用于识别咸鱼之王宝箱的程序", "1.0.0", "xyzw_box")
class XyzwBox(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.ocr_url = config.get("ocr_url", "https://api.ocr.space")
        self.ocr_key = config.get("ocr_key", "")
        self.need_code = config.get("need_code", 3340)
        self.max_retries = config.get("max_retries", 3)

    # 由命令xyzw触发，等待再次输入图片
    # @filter.command("xyzw")
    # async def xyzw(self, event: AstrMessageEvent):
    #     """识别咸鱼之王宝箱"""
    #     sender_id = event.get_sender_id()
    #     logger.info(f"用户 {sender_id} 触发了 xyzw 命令")
    #     yield event.plain_result("请发送一张包含宝箱图片的图片。")
        
    @filter.event_message_type(EventMessageType.ALL)
    async def xyzwocr(self, event: AstrMessageEvent):
        message_chain = event.get_messages()
        if message_chain:
            first_message = message_chain[0]
            try:
                if hasattr(first_message, 'type') and first_message.type == 'Image':
                    logger.info("是图片，开始进行 OCR 识别")
                    image_url = first_message.url
                    logger.info(f"图片 URL: {image_url}")
                    
                    ocr = OCRprocessor(self.ocr_url, self.ocr_key)
                    image_path = await download_image(image_url)
                    text = await ocr.process_image(image_path)
                    logger.info(f"识别结果: {text}")
                    yield event.plain_result(f"代码执行器: 图片识别结果: {text}")
            except Exception as e:
                error_info = traceback.format_exc()
                logger.error(f"OCR 识别出错: {e}")
                print(error_info)
        



class OCRprocessor:
    def __init__(self, ocr_url: str, ocr_key: str):
        self.max_retries = None
        self.ocr_url = ocr_url.rstrip('/')
        self.ocr_key = ocr_key
        self.session = None

    async def initltize(self):
        xyzw = XyzwBox()
        self.max_retries = xyzw.max_retries

    async def init_session(self):
        """初始化异步会话"""
        if not self.session:
            self.session = aiohttp.ClientSession()

    async def close_session(self):
        """关闭异步会话"""
        if self.session:
            await self.session.close()
            self.session = None

    async def process_image(self, image_data: bytes) -> str:
        """处理图片并返回结果"""
        try:
            # 1. 裁剪图片
            cut1_data, cut2_data = await asyncio.to_thread(self._crop_image, image_data)

            # 2. OCR识别
            cut1_text = await self._ocr_request(cut1_data)
            cut2_text = await self._ocr_request(cut2_data)

            # 3. 数据解析
            pre_code = self._parse_pre_code(cut1_text)
            wooden, silver, gold, platinum = self._parse_materials(cut2_text)

            # 4. 计算积分
            return Calculator().calculate(
                wooden, silver, gold, platinum, pre_code
            )
        except Exception as e:
            logger.error(f"OCR处理失败: {str(e)}", exc_info=True)
            raise

    def _crop_image(self, image_data: bytes) -> Tuple[bytes, bytes]:
        """裁剪图片并返回字节数据"""
        try:
            img = Image.open(BytesIO(image_data))
            width, height = img.size
            box_top = (0, int(height * 0.15), int(width * 0.5), int(height * 0.3))
            box_bottom = (0, int(height * 0.75), width, int(height * 0.87))

            # 裁剪并转换为字节
            cut1_img = img.crop(box_top)
            cut2_img = img.crop(box_bottom)

            cut1_bytes = BytesIO()
            cut2_bytes = BytesIO()

            cut1_img.save(cut1_bytes, format='JPEG')
            cut2_img.save(cut2_bytes, format='JPEG')

            return cut1_bytes.getvalue(), cut2_bytes.getvalue()
        except Exception as e:
            logger.error(f"图片裁剪失败: {str(e)}")
            raise

    async def _ocr_request(self, image_data: bytes, retry: int = 0) -> str:
        """发送OCR请求（带重试机制）"""
        url = f"{self.ocr_url}/parse/image"
        data = aiohttp.FormData()
        data.add_field('file', image_data, filename='image.jpg', content_type='image/jpeg')
        data.add_field('apikey', self.ocr_key)
        data.add_field('language', 'chs')
        data.add_field('OCREngine', '2')

        try:
            async with self.session.post(url, data=data) as response:
                if response.status != 200:
                    logger.warning(f"OCR API返回错误状态码: {response.status}")
                    raise RuntimeError(f"OCR API错误: HTTP {response.status}")

                result = await response.json()
                return result["ParsedResults"][0]["ParsedText"]
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            if retry <  self.max_retries:
                logger.warning(f"OCR请求失败，重试 {retry + 1}/{self.max_retries}: {str(e)}")
                await asyncio.sleep(1)
                return await self._ocr_request(image_data, retry + 1)
            raise RuntimeError(f"OCR请求失败: {str(e)}")
        except KeyError:
            logger.error(f"OCR响应格式错误: {json.dumps(result, ensure_ascii=False)[:200]}")
            raise RuntimeError("OCR响应格式错误")

    def _parse_pre_code(self, text: str) -> int:
        """解析预设积分"""
        match = re.search(r"\d+", text)
        if not match:
            raise ValueError("无法解析预设积分")
        return int(match.group())

    def _parse_materials(self, text: str) -> Tuple[int, int, int, int]:
        """解析四种宝箱数量"""
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if len(lines) < 4:
            raise ValueError("OCR结果行数不足")

        cleaned = [
            line[1:].replace("o", "0").replace("O", "0")
            .replace("l", "1").replace("L", "1")
            .replace("S", "5").replace("s", "5")
            .replace("B", "8").replace("b", "6")
            for line in lines[:4]
        ]
        return (
            int(cleaned[0]), int(cleaned[1]),
            int(cleaned[2]), int(cleaned[3])
        )

class Calculator:
    @staticmethod
    def calculate(
            wooden: int,
            silver: int,
            gold: int,
            platinum: int,
            pre_code: int
    ) -> str:
        xyzw = XyzwBox()
        """计算积分结果"""
        need_code = xyzw.need_code
        total = wooden + silver * 10 + gold * 20 + platinum * 50
        adjusted_code = Calculator._adjust_pre_code(pre_code)

        if total >= adjusted_code:
            remaining = total - adjusted_code
            rounds = remaining // need_code
            surplus = need_code - (remaining % need_code)
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

    @staticmethod
    def _adjust_pre_code(pre_code: int) -> int:
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

async def download_image(url: str) -> Optional[bytes]:
    """异步下载图片"""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                if response.status == 200:
                    return await response.read()
                logger.warning(f"图片下载失败: HTTP {response.status}")
                return None
    except Exception as e:
        logger.error(f"图片下载异常: {str(e)}", exc_info=True)
        return None