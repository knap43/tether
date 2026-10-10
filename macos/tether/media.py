"""Media control isn't available on macOS yet; phones are told so and hide their controls."""


class Media:
    available = False

    def __init__(self, on_change):
        self.on_change = on_change

    async def state(self) -> dict:
        return {"players": [], "volume": None, "available": False}

    async def command(self, player, action, value=None) -> dict:
        raise ValueError("media control isn't available on macOS yet")

    async def refresh(self) -> dict:
        return await self.state()

    async def watch(self) -> None:
        pass
