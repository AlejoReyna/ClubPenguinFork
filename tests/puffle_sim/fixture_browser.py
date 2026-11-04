"""Headless Ruffle probe with synthetic accounts and localhost-only requests."""
import json
from pathlib import Path
import sys
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright


def main():
    fixture = json.load(sys.stdin)
    base = fixture['base']
    assert urlparse(base).hostname == '127.0.0.1'
    output = Path(fixture['screenshots'])
    output.mkdir(exist_ok=False)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=['--use-angle=swiftshader'])
        context = browser.new_context(viewport={'width': 1100, 'height': 850})

        def filter_request(route):
            if urlparse(route.request.url).hostname == '127.0.0.1':
                route.continue_()
            else:
                route.abort()

        context.route('**/*', filter_request)
        if hasattr(context, 'route_web_socket'):
            def filter_websocket(ws):
                if urlparse(ws.url).hostname == '127.0.0.1':
                    ws.connect_to_server()
                else:
                    ws.close()
            context.route_web_socket('**/*', filter_websocket)
        page = context.new_page()
        page.goto(base, wait_until='load', timeout=30000)
        page.wait_for_timeout(9000)
        page.screenshot(path=str(output / '01-inicio.png'))
        box = page.locator('#game').bounding_box()
        def click(x, y):
            box = page.locator('#game').bounding_box()
            page.mouse.click(box['x'] + box['width'] * x, box['y'] + box['height'] * y)

        if box:
            click(0.5, 0.86)
            page.wait_for_timeout(4000)
            page.screenshot(path=str(output / '02-clic.png'))
            click(0.55, 0.21)
            page.keyboard.type(fixture['account']['name'], delay=35)
            click(0.55, 0.27)
            page.keyboard.type(fixture['account']['password'], delay=35)
            click(0.5, 0.5)
            page.wait_for_timeout(7000)
            page.screenshot(path=str(output / '03-login.png'))
            click(0.5, 0.225)
            page.wait_for_timeout(9000)
            page.screenshot(path=str(output / '04-mundo.png'))
        browser.close()
    print('Ruffle probe captured synthetic screenshots in ' + str(output) + '; no visual assertion has been made.')


if __name__ == '__main__':
    main()
