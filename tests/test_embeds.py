from unittest.mock import AsyncMock, MagicMock
import pytest
from cogs.qotd.embeds import QotdEmbedsMixin


class DummyQotd(QotdEmbedsMixin):
    def __init__(self):
        self.get_server_language = MagicMock(return_value="en")
        self.get_guild_settings = AsyncMock(return_value={"admin_channel_id": 123456, "language": "en"})
        self.get_qotd_channels = AsyncMock(return_value=[
            {"channel_id": 999, "scheduled_time": "09:00", "role_id": None}
        ])
        self.get_queue_count = AsyncMock(return_value=5)
        mock_cursor = AsyncMock()
        mock_cursor.fetchone.return_value = (5, 10, 2)
        self.db = MagicMock()
        self.db.execute = AsyncMock(return_value=mock_cursor)


@pytest.mark.asyncio
async def test_build_settings_embed():
    qotd = DummyQotd()
    embed = await qotd.build_settings_embed(guild_id=1, lang="en")
    assert embed is not None
    assert "Settings" in embed.title or "Nastavení" in embed.title
    # Verify fields were rendered
    field_names = [f.name for f in embed.fields]
    assert any("Admin" in name for name in field_names)
    assert any("Language" in name for name in field_names)
    assert any("Channels" in name or "kanál" in name.lower() for name in field_names)
    assert any("Database" in name or "databáz" in name.lower() for name in field_names)
