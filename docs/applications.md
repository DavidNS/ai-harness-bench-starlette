
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

Pass `max_body_size` (in bytes) to limit the size of request bodies. Larger requests receive a
`413` response with the plain-text body `Content Too Large`. The default, `None`, means no limit.

```python
app = Starlette(routes=routes, max_body_size=10 * 1024 * 1024)
```

`Router`, `Mount` and `Route` accept the same option and override the limit of whatever contains
them, so they can raise or lower it for their part of the application:

```python
routes = [
    Route("/avatar", upload_avatar, methods=["POST"], max_body_size=5 * 1024 * 1024),
    Mount("/api", routes=api_routes, max_body_size=64 * 1024),
]
app = Starlette(routes=routes, max_body_size=1024 * 1024)
```

The limit counts the body bytes received from the server, multipart file data and encoding
overhead included. A `Content-Length` header over the limit is rejected as soon as routing
has decided which limit applies (the matched route's, otherwise the enclosing mount, router or
application's), before the endpoint runs. A missing or understated header does not let a larger body
through. If the response has already started, no second response is sent and the request body stream is stopped.

The same limit is available for any ASGI application as
[`RequestBodyLimitMiddleware`](middleware.md#requestbodylimitmiddleware).
