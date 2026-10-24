
??? abstract "API Reference"
    ::: starlette.applications.Starlette
        options:
            parameter_headings: false
            show_root_heading: true
            heading_level: 3
            filters:
                - "__init__"

Starlette includes an application class `Starlette` that nicely ties together all of
its other functionality.

```python
from contextlib import asynccontextmanager

from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route, Mount, WebSocketRoute
from starlette.staticfiles import StaticFiles


def homepage(request):
    return PlainTextResponse('Hello, world!')

def user_me(request):
    username = "John Doe"
    return PlainTextResponse('Hello, %s!' % username)

def user(request):
    username = request.path_params['username']
    return PlainTextResponse('Hello, %s!' % username)

async def websocket_endpoint(websocket):
    await websocket.accept()
    await websocket.send_text('Hello, websocket!')
    await websocket.close()

@asynccontextmanager
async def lifespan(app):
    print('Startup')
    yield
    print('Shutdown')


routes = [
    Route('/', homepage),
    Route('/user/me', user_me),
    Route('/user/{username}', user),
    WebSocketRoute('/ws', websocket_endpoint),
    Mount('/static', StaticFiles(directory="static")),
]

app = Starlette(debug=True, routes=routes, lifespan=lifespan)
```

### Storing state on the app instance

You can store arbitrary extra state on the application instance, using the
generic `app.state` attribute.

For example:

```python
app.state.ADMIN_EMAIL = 'admin@example.org'
```

### Accessing the app instance

Where a `request` is available (i.e. endpoints and middleware), the app is available on `request.app`.

### Limiting the request body size

`Starlette`, `Router`, `Mount` and `Route` accept a `max_body_size` keyword argument, the maximum
number of request body bytes in the request. Larger requests get a `413` response with the body
`Content Too Large`. The default `None` means unlimited.

```python
from starlette.applications import Starlette
from starlette.routing import Mount, Route

app = Starlette(
    routes=[
        Route("/avatar", upload_avatar, methods=["POST"], max_body_size=5 * 1024 * 1024),
        Mount("/api", routes=api_routes, max_body_size=64 * 1024),
    ],
    max_body_size=1024 * 1024,
)
```

The limit of the application applies before routing. A limit set on an inner route, mount or router
replaces the outer one for that part of the tree, so it can be higher or lower. The bytes received from the server
are counted. A `Content-Length` over the limit is rejected as soon as routing has settled which limit applies,
before the endpoint runs. For a request matching no route or method, that is the limit of the enclosing application,
mount or router.

The same behaviour is available for any ASGI application with
[`RequestBodyLimitMiddleware`](middleware.md#requestbodylimitmiddleware).
