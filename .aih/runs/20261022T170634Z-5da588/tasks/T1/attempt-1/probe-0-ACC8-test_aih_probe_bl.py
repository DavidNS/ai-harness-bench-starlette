import anyio
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Mount, Route, Router
from starlette.testclient import TestClient

ran = []

async def ep(request: Request):
    ran.append(1)
    return PlainTextResponse(str(len(await request.body())))

def test_mount_cl_raise_and_lower():
    inner = Router(routes=[Route("/", ep, methods=["POST"], max_body_size=100)])
    app = Starlette(routes=[Mount("/m", app=inner), Mount("/n", routes=[Route("/", ep, methods=["POST"])], max_body_size=5)], max_body_size=5)
    c = TestClient(app)
    assert c.post("/m/", content=b"x" * 50).text == "50"
    ran.clear()
    assert c.post("/n/", content=b"x" * 10).status_code == 413
    assert ran == []
