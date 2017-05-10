#!/bin/sh
# Prerender the loaded state of web pages and record resource times.
# Copyright 2017 Chad Miller <chad@cornsilk.net>

""":" # First, a little wrapper to download and import dependencies.
if test -d env; then
    . env/bin/activate
else
    python3 -m venv env
    . env/bin/activate
    python3 -m pip install --upgrade pip
    python3 -m pip install aiohttp requests websockets
fi

#TODO: make sure chrome/chromium is installed.

exec python3 "$0" "$@"
"""

import codecs
import functools
import json
import logging as loggingmod
import os
import shutil
import subprocess
import sys
import tempfile
import time

import aiohttp.web
import requests  # TODO(chad): consider using aiohttp client
import websockets  # TODO(chad): consider using aiohttp client websockets

logger = loggingmod.getLogger(__name__)

PRERENDER_STORAGE = os.environ.get("PRERENDER_STORAGE", "/var/tmp")

FORM_FMT = """<html><style>body {{ margin: 10vmin; }} #error {{ color: red; }} #error:empty, #results:empty {{ display: none; }}</style><body><p id="error">{0}</p><form><input required type="url" name="url" value="{1}" placeholder="http://example.com/"><input type="submit" value="prerender this URL"></form><p id="results">{2}</p></html>"""


async def process_page_and_store(url, cache_filename, port):
    """Uses browser to download a page and wait for Loaded signal. Store
    information about rendered HTML and resource usage."""

    new_rpc_url = "http://localhost:{0}/json/new".format(port)
    new_tab_info_page = requests.get(new_rpc_url)

    assert new_tab_info_page.ok, new_rpc_url

    new_tab_info = new_tab_info_page.json()
    close_rpc_url = "http://localhost:{0}/json/close/{1}".format(port, new_tab_info['id'])

    metadata = {"timings": []}
    page = None
    discarded_lines = set()

    async with websockets.connect(new_tab_info["webSocketDebuggerUrl"]) as ws:
        try:
            # Required to receive Page.loadEventFired events
            await ws.send("""{ "id": 1, "method": "Page.enable" }""")
            # Required to receive Network.responseReceived events
            await ws.send("""{ "id": 2, "method": "Network.enable" }""")
            await ws.send("""{ "id": 3, "method": "Page.navigate", "params": { "url": "%s" } }"""%(url,))

            done = False
            while not done:
                message = await ws.recv()
                if message:
                    packet = json.loads(message)

                    if "error" in packet:
                        logger.warning("Received error packet: %s", 
                                json.dumps(packet, indent=4, sort_keys=True))
                        continue

                    if "method" in packet:
                        if packet["method"] == "Network.responseReceived":
                            response = packet["params"]["response"]

                            if response["protocol"] == "data":
                                # phony network request.
                                continue

                            # This is bad. Instead, should track redirection
                            # and catch the network resource info that matches
                            # the final URL.
                            if "content-type" not in metadata:
                                # save the content type for replaying later.
                                metadata["Content-Type"] = response["headers"].get("content-type")

                            # Save everything we load because of this URL.
                            metadata["timings"].append([response["url"], packet["params"]["timestamp"] - response["timing"]["requestTime"]])
                            continue

                        if packet["method"] == "Page.loadEventFired":
                            await ws.send("""{ "id": 4, "method": "Runtime.enable" }""")
                            await ws.send("""{ "id": 5, "method": "Runtime.evaluate", "params": { "expression": "document.body.parentElement.outerHTML" } }""")
                            continue

                        discarded_lines.add("method {0}".format(packet["method"]))

                    if "result" in packet:
                        if packet["id"] == 5:
                            page = packet["result"]["result"]["value"]
                            done = True
                            await ws.send("""{ "id": 6, "method": "Page.navigate", "params": { "url": "about:blank" } }""")
                            continue

                        if packet["result"]:
                            discarded_lines.add("result for id {0}".format(packet["id"]))
                            continue

            if discarded_lines:
                logger.warning("Browser lines not handled: %s", discarded_lines)

            temp_fileno, temp_name = tempfile.mkstemp(prefix="temp-", dir=os.path.dirname(cache_filename))
            try:
                with os.fdopen(temp_fileno, "w") as scratchpad:
                    # first line, metadata
                    json.dump(metadata, scratchpad, indent=None)
                    scratchpad.write("\n")
                    # remainder, page contents
                    scratchpad.write(page)

                os.rename(temp_name, cache_filename)
            finally:
                pass
                # EBADFILE? Already closed? TODO(chad): strace this
                #os.close(temp_fileno)

        finally:
            requests.get(close_rpc_url)

async def get_url_prepared(port, request):
    given_url_to_process = request.query.get("url")
    if not given_url_to_process:
        return None, None

    escaped_url = codecs.encode(given_url_to_process.encode("UTF8"), "base64").decode("ASCII").strip()
    processed_filename = os.path.join(PRERENDER_STORAGE, "data-" + escaped_url)
    if not os.path.exists(processed_filename):
        await process_page_and_store(given_url_to_process, processed_filename, port)

    return given_url_to_process, processed_filename

async def handle_front(port, request):
    url, _ = await get_url_prepared(port, request)

    if not url:
        return aiohttp.web.Response(text=FORM_FMT.format("", "", ""), content_type="text/html")

    return aiohttp.web.Response(text=FORM_FMT.format("", url, """
            <a href="{0}">rendered</a>,
            <a href="{1}">dependency timing</a>""".format(request.app.router["rendered"].url_for().with_query(url=url), request.app.router["timings"].url_for().with_query(url=url))), content_type="text/html")

async def handle_get_rendered(port, request):
    url, processed_filename = await get_url_prepared(port, request)
    if not url:
        return aiohttp.web.json_response({"error": "required 'url' parameter"})

    with open(processed_filename) as f:
        first_line = f.readline()
        metadata = json.loads(first_line)
        return aiohttp.web.json_response({"for": url, "resources": str(request.app.router["timings"].url_for().with_query(url=url)), "content-type": metadata.get("Content-Type"), "page": f.read()})

async def handle_get_timings(port, request):
    url, processed_filename = await get_url_prepared(port, request)
    if not url:
        return aiohttp.web.json_response({"error": "required 'url' parameter"})

    with open(processed_filename) as f:
        first_line = f.readline()
        metadata = json.loads(first_line)
        return aiohttp.web.json_response({"for": url, "rendered": str(request.app.router["rendered"].url_for().with_query(url=url)), "resource-timing": metadata.get("timings")})


if __name__ == "__main__":

    loggingmod.basicConfig(level=loggingmod.INFO)
    browser_home = tempfile.mkdtemp()
    try:

        # TODO(chad): Make this into a pool of chromes.
        for command in ["chromium-browser", "chrome", "google-chrome"]:
            command_full_path = shutil.which(command)
            if not command_full_path:
                continue
            browser = subprocess.Popen(
                    [command_full_path,
                        # Headless disabled.  https://bugs.chromium.org/p/chromium/issues/detail?id=696198
                        #"--headless",
                        "--incognito",
                        "--no-default-browser-check",
                        "--no-first-run",
                        "--remote-debugging-port=0",  # bind() dynamic-port hack.
                        "--disable-gpu",  # Mesa bug? Try without one day.
                        "--user-data-dir=" + browser_home], stderr=subprocess.DEVNULL)
            # This port=zero is an undocumented feature of chromium and could
            # change in the future. This depends on the bind() syscall behabior
            # to tell the OS to assign any port that it can. It prevents us
            # from trying to occupy an already-used port, but might break one
            # day.
            break
        else:
            logger.error("No chrome/chromium was in your $PATH.")
            sys.exit(1)

        devtool_port_file = os.path.join(browser_home, "DevToolsActivePort")
        for retry in range(30):
            if os.path.exists(devtool_port_file) and os.path.getsize(devtool_port_file) > 0:
                break
            time.sleep(1)
        else:
            logger.error("Remote debugging console didn't open in time.")
            sys.exit(1)

        devtool_port = int(open(devtool_port_file).read())

        app = aiohttp.web.Application()
        app.router.add_get('/', functools.partial(handle_front, devtool_port))
        app.router.add_get('/rendered', functools.partial(handle_get_rendered, devtool_port), name="rendered")
        app.router.add_get('/timing', functools.partial(handle_get_timings, devtool_port), name="timings")

        aiohttp.web.run_app(app)

    finally:
        if browser:
            browser.kill()
            browser.wait()
        shutil.rmtree(browser_home)
