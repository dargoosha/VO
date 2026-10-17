from aiohttp import web


async def noop(request):
    return web.Response(
        text="ok",
        content_type="text/plain"
    )


app = web.Application()
app.router.add_get("/noop", noop)

if __name__ == "__main__":
    web.run_app(
        app,
        host="127.0.0.1",
        port=9000,
        access_log=None
    )