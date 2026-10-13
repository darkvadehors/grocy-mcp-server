import httpx2
import pytest

from grocy_mcp.cimd import CimdError, CimdFetcher

pytestmark = pytest.mark.anyio
URL = "https://claude.ai/oauth/mcp-client-metadata.json"
DOC = {"client_id": URL, "client_name": "Claude", "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"]}


def resolver(*addresses):
    async def resolve(host):
        return list(addresses)

    return resolve


def fetcher(handler, *addresses):
    return CimdFetcher(("claude.ai",), transport=httpx2.MockTransport(handler), resolver=resolver(*(addresses or ("160.79.104.10",))))


async def test_valid_document():
    assert await fetcher(lambda r: httpx2.Response(200, json=DOC)).fetch(URL) == DOC


@pytest.mark.parametrize(
    "url",
    [
        "http://claude.ai/x.json",
        "https://claude.ai:8443/x.json",
        "https://evil.example/x.json",
        "https://user@claude.ai/x.json",
        "https://claude.ai/x.json?a=1",
        "https://127.0.0.1/x.json",
    ],
)
async def test_url_refused(url):
    with pytest.raises(CimdError):
        await fetcher(lambda r: httpx2.Response(200, json=DOC)).fetch(url)


@pytest.mark.parametrize("address", ["127.0.0.1", "169.254.169.254", "10.0.0.5", "::1", "fe80::1", "192.168.1.2"])
async def test_private_resolution_refused(address):
    with pytest.raises(CimdError, match="adresse"):
        await fetcher(lambda r: httpx2.Response(200, json=DOC), address).fetch(URL)


async def test_redirect_size_and_identity_refused():
    with pytest.raises(CimdError):
        await fetcher(lambda r: httpx2.Response(302, headers={"Location": "http://169.254.169.254/"})).fetch(URL)
    with pytest.raises(CimdError, match="volumineux"):
        await fetcher(lambda r: httpx2.Response(200, content=b"{" + b" " * 6000 + b"}")).fetch(URL)
    with pytest.raises(CimdError, match="client_id"):
        await fetcher(lambda r: httpx2.Response(200, json=dict(DOC, client_id="https://claude.ai/autre"))).fetch(URL)
    with pytest.raises(CimdError):
        await fetcher(lambda r: httpx2.Response(200, content=b"pas du json")).fetch(URL)


async def test_timeout():
    def slow(request):
        raise httpx2.ReadTimeout("lent", request=request)

    with pytest.raises(CimdError):
        await fetcher(slow).fetch(URL)
