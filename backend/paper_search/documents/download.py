"""HTTP adapter for bounded, cancellable PDF downloads."""

import httpx


class HttpDocumentDownloader:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def fetch(self, url: str, max_bytes: int) -> bytes:
        data = bytearray()
        async with self.client.stream("GET", url, follow_redirects=True) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > max_bytes:
                    raise ValueError("PDF 超过配置的大小限制")
        return bytes(data)
