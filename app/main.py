import os
import httpx
import base64
import logging
import logging.handlers
from asyncio import gather
from dotenv import load_dotenv
from fastapi import FastAPI, Response, HTTPException
import re
from urllib.parse import unquote, quote


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

def format_bytes(bytes_value: int) -> str:
    '''Convert bytes to human-readable format (GB, MB, KB)'''
    if bytes_value >= 1_073_741_824:  # >= 1GB
        return f"{bytes_value / 1_073_741_824:.2f}GB"
    elif bytes_value >= 1_048_576:  # >= 1MB
        return f"{bytes_value / 1_048_576:.2f}MB"
    elif bytes_value >= 1024:  # >= 1KB
        return f"{bytes_value / 1024:.2f}KB"
    else:
        return f"{bytes_value}B"

def parse_traffic_from_userinfo(userinfo: str) -> str:
    '''
    Parse subscription-userinfo header and return formatted traffic string.
    Example input: 'upload=235205700; download=1883111384; total=0; expire=0'
    Example output: '↑235.21MB ↓1.88GB'
    '''
    try:
        parts = dict(item.split('=') for item in userinfo.split('; '))
        download = int(parts.get('download', 0))
        
        download_str = format_bytes(download)
        
        return f"↓{download_str}"
    except Exception as e:
        logger.warning(f"Failed to parse userinfo: {e}")
        return ""

def clean_link_name(link: str, traffic_suffix: str = "") -> str:
    '''
    Remove email suffix from VLESS/VMess/Trojan link fragments.
    Keeps emoji and inbound name, removes the email part.
    '''
    try:
        # find the fragment (after #)
        hash_idx = link.rfind('#')
        if hash_idx == -1:
            return link
        
        fragment = link[hash_idx + 1:]
        decoded_fragment = unquote(fragment)

        logger.info(f"Before cleaning: {decoded_fragment}")
        
        # remove email pattern
        cleaned = re.sub(r'-[a-zA-Z0-9]+(?:-[\dDHM,]+)?(?:⏳|⌛)?$', '', decoded_fragment)

        if traffic_suffix:
            cleaned = f"{cleaned} {traffic_suffix}"

        logger.info(f"After cleaning: {cleaned}")
        
        # re-encode and rebuild the link
        encoded_fragment = quote(cleaned, safe='')
        return link[:hash_idx + 1] + encoded_fragment
        
    except Exception as e:
        logger.warning(f"Failed to clean link name: {e}")
        return link


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
        data_with_headers = [(content, headers) for content, headers in valid_results] if valid_results else []

        # merge subscription metadata from first available source (for global headers)
        merged_headers = {}
        if data_with_headers:
            first_headers = data_with_headers[0][1]
            for key in ['subscription-userinfo', 'profile-update-interval', 'profile-web-page-url']:
                if key in first_headers:
                    merged_headers[key] = first_headers[key]

        # clean email from profile and add traffic info per VPS
        cleaned_data = []
        for content, headers in data_with_headers:
            traffic_suffix = ""
            if 'subscription-userinfo' in headers:
                traffic_suffix = parse_traffic_from_userinfo(headers['subscription-userinfo'])
            
            lines = content.decode('utf-8').splitlines()
            cleaned_lines = [clean_link_name(line, traffic_suffix) for line in lines]
            cleaned_data.append('\n'.join(cleaned_lines).encode('utf-8'))


        vless_traffic_suffix = ""
        
        cleaned_vless = [clean_link_name(link, vless_traffic_suffix).encode() for link in vless_links]

        # merge content
        merged_subs = b'\n'.join(cleaned_data) if cleaned_data else b''
        merged_configs = b'\n'.join(cleaned_vless)

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
