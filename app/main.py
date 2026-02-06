import os
import httpx
import base64
import logging
import logging.handlers
from asyncio import gather
from dotenv import load_dotenv
from fastapi import FastAPI, Response, HTTPException


# logging configuration with rotation every 3 days
logger_file = logging.handlers.TimedRotatingFileHandler(
    filename="py.log",
    when="midnight",
    interval=3,
    backupCount=5   # keep up to 5 log files
)
formatter = logging.Formatter(
    fmt='[{asctime}] #{levelname:8} {filename}:{lineno} - {message}',
    style='{'
)
logger_file.setFormatter(formatter)
logger = logging.getLogger()
logger.handlers.clear()
logger.setLevel(logging.INFO)
logger.addHandler(logger_file)


# initialize FastAPI app
app = FastAPI()

# Load environs
load_dotenv()


async def fetch_links() -> tuple[list[str], list[str]]:
    '''
    Fetches the configuration source file from GitHub or local .txt file.\n
    Returns a tuple of two lists:
        - HTTP subscription links
        - Direct vless configuration links
    '''
    try:
        if os.getenv('LOCAL_MODE') == 'on':
            with open('configs.txt', encoding='utf-8') as file:
                lines = file.readlines()

        else:
            github_token = os.getenv('GITHUB_TOKEN')
            headers = {}
            # If token is provided, use it for private repo access
            if github_token:
                headers = {
                    "Authorization": f"token {github_token}",
                    "Accept": "application/vnd.github.v3.raw"
                }
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    os.getenv('CONFIG_URL'),
                    headers=headers,
                    timeout=6
                )
                response.raise_for_status()
                lines = response.text.splitlines()
            
        sub_links = [
            line.strip()
            for line in lines
            if line.strip().startswith('http')
        ]
        vless_links = [
            line.strip()
            for line in lines
            if line.strip().startswith('vless://')
        ]
            
        return sub_links, vless_links
    except httpx.HTTPStatusError as e:
        logger.critical(f"GitHub fetch error: {str(e)}")
        raise HTTPException(
            status_code=404,
            detail="Config file not found"
        )
    except FileNotFoundError as e:
        logger.critical(e)
        raise e

async def fetch_subscription(
    client: httpx.AsyncClient,
    sub_link: str,
    sub_id: str
) -> tuple[bytes, dict] | None:
    '''
    Downloads subscription config and captures headers.
    Returns (decoded_content, headers) or None if failed.
    '''
    try:
        sub = await client.get(f'{sub_link}{sub_id}', timeout=3)
        sub.raise_for_status()
        logger.info(f"Headers from {sub_link}: {dict(sub.headers)}")
        return base64.b64decode(sub.text), dict(sub.headers)
    except httpx.HTTPError as e:
        logger.warning(f"Can't get subscription from {sub_link}{sub_id}: {str(e)}")
        return None

async def merge_all(sub_links: list[str], vless_links: list[str], sub_id: str) -> tuple[bytes, dict]:
    '''
    Returns (merged_content, merged_headers)
    '''
    async with httpx.AsyncClient() as client:
        decoded_subs = [
            fetch_subscription(client, sub_url, sub_id)
            for sub_url in sub_links
        ]
        tmp = await gather(*decoded_subs)
        valid_results = [x for x in tmp if x is not None]

        if not valid_results and not vless_links:
            logger.error("No subscriptions or configurations available")
            raise HTTPException(status_code=500, detail="There is nothing to return")

        # extract content and headers
        data = [content for content, _ in valid_results] if valid_results else []
        headers_list = [headers for _, headers in valid_results] if valid_results else []

        # merge subscription metadata from first available source
        merged_headers = {}
        if headers_list:
            first_headers = headers_list[0]
            # copy relevant subscription headers (EXCLUDING profile-title since we'll set it ourselves)
            for key in ['subscription-userinfo', 'profile-update-interval', 'profile-web-page-url']:
                if key in first_headers:
                    merged_headers[key] = first_headers[key]

        # merge content
        encoded_vless_links = [link.encode() for link in vless_links]
        merged_subs = b'\n'.join(data) if data else b''
        merged_configs = b'\n'.join(encoded_vless_links)

        return merged_subs + merged_configs, merged_headers

path = os.getenv('URL')


@app.get(f'/{path}/{{sub_id}}')
@app.get(f'/{path}')
async def main(sub_id: str = "") -> Response:
    sub_links, vless_links = await fetch_links()
    if not sub_links and not vless_links:
        logger.error("No subscriptions or configurations available")
        raise HTTPException(status_code=500, detail="There is nothing to return")

    result, headers = await merge_all(sub_links, vless_links, sub_id)
    global_sub = base64.b64encode(result)

    sub_name = os.getenv('SUB_NAME', 'service')
    headers['profile-title'] = sub_name
    headers['content-disposition'] = f'attachment; filename="{sub_name}"'


    return Response(
        content=global_sub,
        media_type='text/plain',
        headers=headers  
    )
