
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

### Limiting the request body size

Pass `max_body_size` (in bytes) to limit the size of HTTP request bodies. Requests over the limit get a
`413` response with the plain-text body `Content Too Large`. The default, `None`, means unlimited.

```python
app = Starlette(routes=routes, max_body_size=1024 * 1024)
```

The same option is accepted by `Router`, `Mount` and `Route`. A limit set on an inner route, mount or router
replaces the outer one for that part of the application, so it can raise as well as lower the
application-wide limit:

```python
routes = [
    Route('/note', create_note),  # 1 MiB, from the application
    Route('/avatar', upload_avatar, methods=['POST'], max_body_size=10 * 1024 * 1024),
    Mount('/hooks', routes=hook_routes, max_body_size=4096),
]
app = Starlette(routes=routes, max_body_size=1024 * 1024)
```

The limit counts the body bytes received from the server, including multipart file data and encoding overhead.
`Content-Length` is only used to reject early: once routing has settled which limit applies, a declared length
over it is rejected before the endpoint runs. A missing or understated header doesn't let a larger body through.
Websockets and lifespan are not affected. See also
[`RequestBodyLimitMiddleware`](middleware.md#requestbodylimitmiddleware) for use with any ASGI application.

### Storing state on the app instance

You can store arbitrary extra state on the application instance, using the
generic `app.state` attribute.

For example:

```python
app.state.ADMIN_EMAIL = 'admin@example.org'
```

### Accessing the app instance

Where a `request` is available (i.e. endpoints and middleware), the app is available on `request.app`.
