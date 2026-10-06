"""Discord Bot 适配器"""
from typing import Dict, Any, Optional

from .message import BotMessage
from .platform_adapter import BasePlatformAdapter, PlatformType
from ..utils.logger import get_logger

logger = get_logger("DiscordAdapter")


class DiscordBotAdapter(BasePlatformAdapter):
    """
    Discord Bot 适配器

    使用 discord.py 库连接 Discord Gateway。
    支持：私聊、频道、富文本嵌入、图片、回复消息。

    配置示例：
        config = {
            "token": "Bot MTIz...",
            "admin_ids": ["123456789"],
            "prefix": "!",
            "intents": ["guilds", "messages", "message_content"],
        }
    """

    DEFAULT_CONFIG = {
        "token": "",
        "admin_ids": [],
        "prefix": "!",
        "intents": ["guilds", "messages", "message_content"],
    }

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        self._token = self._config.get("token", "")
        self._admin_ids = self._config.get("admin_ids", [])
        self._prefix = self._config.get("prefix", "!")
        self._discord_bot = None

    @property
    def platform_type(self) -> str:
        return PlatformType.DISCORD.value

    @property
    def supports_multimodal(self) -> bool:
        return True

    @property
    def name(self) -> str:
        return "Discord"

    async def start(self) -> None:
        """启动 Discord Bot"""
        if not self._token:
            raise ValueError("Discord token is required")

        try:
            import discord
        except ImportError:
            raise ImportError(
                "discord.py is required for Discord adapter. "
                "Install: pip install discord.py"
            )

        await super().start()

        # 配置 intents
        intents = discord.Intents.default()
        intent_list = self._config.get("intents", [])
        for i in intent_list:
            if hasattr(intents, i):
                setattr(intents, i, True)

        self._discord_bot = discord.Bot(intents=intents, command_prefix=self._prefix)

        # 注册消息处理器
        @self._discord_bot.event
        async def on_message(msg):
            if msg.author == self._discord_bot.user:
                return

            bot_msg = self._parse_discord_message(msg)
            if bot_msg is None:
                return

            result = await self._dispatch_message(bot_msg)
            if result:
                embed = discord.Embed(
                    description=result[:4000],
                    color=0x7C3AED,  # 紫色
                )
                reply_kwargs = {}
                if bot_msg.reply_to:
                    reply_kwargs["reference"] = msg.reference
                await msg.channel.send(embed=embed, **reply_kwargs)

        @self._discord_bot.event
        async def on_ready():
            logger.info(f"Discord Bot started as {self._discord_bot.user}")

        # 在后台运行 discord bot
        self._task = asyncio.create_task(self._discord_bot.start(self._token))

    async def stop(self) -> None:
        await super().stop()
        if self._discord_bot:
            await self._discord_bot.close()

    async def send_message(
        self,
        target_id: str,
        content: str,
        images: Optional[list] = None,
        reply_to: Optional[str] = None,
        **kwargs
    ) -> bool:
        if not self._discord_bot:
            return False
        try:
            channel = self._discord_bot.get_channel(int(target_id))
            if not channel:
                channel = await self._discord_bot.fetch_channel(int(target_id))

            embed = discord.Embed(description=content[:4000], color=0x7C3AED)
            if images:
                embed.set_image(url=images[0])
            await channel.send(embed=embed)
            return True
        except Exception as e:
            logger.error(f"Failed to send message: {e}")
            return False

    def _parse_discord_message(self, discord_msg) -> Optional[BotMessage]:
        """解析 Discord 消息为统一格式"""
        import time
        author = discord_msg.author
        content = discord_msg.content.strip()

        # 检查引用
        reply_to = None
        if discord_msg.reference:
            reply_to = str(discord_msg.reference.message_id) if discord_msg.reference.message_id else None

        # 检查是否 @Bot
        at_bot = False
        if self._discord_bot and self._discord_bot.user:
            if discord_msg_mentions := discord_msg.mentions:
                for m in discord_msg_mentions:
                    if m == self._discord_bot.user:
                        at_bot = True
                        break

        msg = BotMessage(
            platform=self.platform_type,
            platform_type=self.platform_type,
            message_id=str(discord_msg.id),
            timestamp=discord_msg.created_at.timestamp(),
            user_id=str(author.id),
            username=author.display_name,
            content=content,
            group_id=str(discord_msg.channel.id) if not isinstance(discord_msg.channel, discord.DMChannel) else None,
            is_group=not isinstance(discord_msg.channel, discord.DMChannel),
            reply_to=reply_to,
            at_bot=at_bot,
        )

        # 提取图片
        if discord_msg.attachments:
            msg.images = [a.url for a in discord_msg.attachments if a.content_type and "image" in a.content_type]

        return msg
