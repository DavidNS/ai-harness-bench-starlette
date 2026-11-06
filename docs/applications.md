
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

By default a client can send a request body of any size. Pass `max_body_size` to `Starlette`
to limit it, in bytes:

```python
app = Starlette(routes=routes, max_body_size=1024 * 1024)  # 1 MB
```

The `Router`, `Mount` and `Route` classes accept the same keyword. A limit set on an inner
route, mount or router replaces the outer one for that part of the application, so it can
raise or lower the application-wide limit:

```python
routes = [
    Route('/comments', create_comment, methods=['POST']),  # 1 MB
    Route('/upload', upload, methods=['POST'], max_body_size=100 * 1024 * 1024),  # 100 MB
    Mount('/webhooks', routes=webhook_routes, max_body_size=64 * 1024),  # 64 KB
]
app = Starlette(routes=routes, max_body_size=1024 * 1024)
```

A request that crosses the limit gets a `413` response with the plain-text body `Content Too Large`.
If the response has already started, it is cut off instead, and the request body stream stops:
`request.stream()` and `request.body()` raise `RequestBodyTooLarge`
(from `starlette.middleware.body_limit`).

The limit counts the body bytes received from the server, multipart file data and encoding overhead
included, whatever the `Content-Length` header says. A declared `Content-Length` over the limit is
rejected without reading the body, as soon as the limit that applies is known: when routing has
decided, before the endpoint of the matched route runs.

Websockets, lifespan and requests without a body are not affected. To use the limit outside of
Starlette, see [`RequestBodyLimitMiddleware`](middleware.md#requestbodylimitmiddleware).

The body that middleware has read before routing is counted again against the limit of the route that
is chosen. `max_body_size` must
be an `int` (or `None`). A declared `Content-Length` is checked as soon as nothing behind the current
route, mount or host can change the limit; for an application that may set limits of its own, it is
checked on its first read of the body. An application that hides a router or `Starlette` application
from the `.app` attribute chain (a function wrapping one, for instance) counts as a plain ASGI app.

Before routing, a middleware that reads the body is held to the limit of the route the request is
headed for, found by matching the path the server sent (if matching raises, by the limit of the
application). A path that matches no route, or a route with the
wrong method, is held to the limit of the application (or of the mount or router reached), never to the
largest limit of the application. This is approximate when the middleware changes what routing sees:
a middleware that rewrites the path and reads the body before routing is held to the limit of the path
the client sent. To give such a path a different limit, set `max_body_size` on a `Mount` for the external
prefix. The limit of the route that is finally chosen is always applied to the bytes read so far.
