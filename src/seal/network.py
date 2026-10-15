"""Public-address DNS boundary shared by collection and source discovery."""

import aiohttp

from .core import check_address


class Resolver(aiohttp.resolver.DefaultResolver):
    async def resolve(self, host, port=0, family=0):
        addresses = await super().resolve(host, port, family)
        for address in addresses:
            check_address(address["host"])
        return addresses
