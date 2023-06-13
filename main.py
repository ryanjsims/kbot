from requests_toolbelt import MultipartEncoder
import requests
import os
import re
import json
import shutil
from time import sleep, time
from datetime import datetime
from dateutil.parser import *
from dateutil.tz import tzutc
from argparse import ArgumentParser
from dotenv import load_dotenv
from rss_parser import Parser as RSSParser
from typing import Optional, List
import sys
from io import BytesIO
import logging
import http.client as http_client
import string
import random

logging.basicConfig()
logger = logging.getLogger("kbot")
logger.setLevel(logging.DEBUG)

load_dotenv()

TOKEN_REGEX = re.compile('"(_csrf_token|entry_link\[_token\])"\s+value="(.+)"')
MAGAZINE_REGEX = re.compile('"entry_link\[magazine\]\[autocomplete\]".+value="([0-9]+)"\sselected="selected"')

KBOT_USER = os.getenv("KBOT_USER")
KBOT_PASS = os.getenv("KBOT_PASS")
KBOT_INSTANCE = os.getenv("KBOT_INSTANCE")
KBOT_MAGAZINE = os.getenv("KBOT_MAGAZINE")
KBOT_RSS = os.getenv("KBOT_RSS")
KBOT_LANG = os.getenv("KBOT_LANG")

KBOT_FREQUENCY = max(120, int(os.getenv("KBOT_FREQUENCY", "600")))

assert KBOT_USER and KBOT_PASS and KBOT_INSTANCE and KBOT_MAGAZINE and KBOT_RSS and KBOT_LANG, "Environment not set up correctly!"

cache_name = ".last-updated"
logged_in = False

def login_hook(r: requests.Response, *args, **kwargs):
    global logged_in
    if r.history and "login" in r.url:
        logger.info(f"Redirected to login page: {r.status_code} {r.url}")
        logged_in = False


def rate_limit_hook(r: requests.Response, *args, **kwargs):
    global last_request_time
    poll_latency = 1.0

    now = time()
    if now < poll_latency + last_request_time:
        sleep(min(poll_latency, poll_latency + last_request_time > now))
    last_request_time = time()

def get_session():
    global last_request_time

    last_request_time = time() - 100
    session = requests.Session()
    session.hooks['response'].append(rate_limit_hook)
    session.hooks['response'].append(login_hook)
    return session

kbin_session = get_session()

def get_csrf(response: requests.Response) -> Optional[str]:
    match = TOKEN_REGEX.search(response.text)
    if not match:
        logger.error("Could not find csrf token!")
        return None
    return match.group(2)

def login() -> bool:
    global logged_in
    response = kbin_session.get(f"https://{KBOT_INSTANCE}/login")
    if(not (200 <= response.status_code < 300)):
        logger.error(f"Unexpected status code: {response.status_code}")
        return False
        
    _csrf_token = get_csrf(response)

    if not _csrf_token:
        return False
    
    form_data = {
        "email": KBOT_USER,
        "password": KBOT_PASS,
        "_csrf_token": _csrf_token
    }

    response = kbin_session.post(f"https://{KBOT_INSTANCE}/login", data=form_data)
    if response.status_code not in [200, 302]:
        logger.error(f"Unexpected status code: {response.status_code}")
        return False

    logged_in = True
    return True

def get_magazine(response: requests.Response) -> int:
    match = MAGAZINE_REGEX.search(response.text)
    if not match:
        logger.error("Could not find magazine id!")
        return -1
    return int(match.group(1))

def post_link(link: str, title: str, description: Optional[str] = None, tags: Optional[List[str]] = None) -> bool:
    response = kbin_session.get(f"https://{KBOT_INSTANCE}/m/{KBOT_MAGAZINE}/new")
    if(not (200 <= response.status_code < 300)):
        logger.error(f"Unexpected status code: {response.status_code}")
        return False
    
    _csrf_token = get_csrf(response)

    if not _csrf_token:
        return False
    
    magazine_id = get_magazine(response)

    if magazine_id == -1:
        return False

    form_data = {
        "entry_link[url]": link,
        "entry_link[title]": title,
        "entry_link[body]": description if description is not None else "",
        "entry_link[magazine][autocomplete]": str(magazine_id),
        "entry_link[tags]": ",".join(tags) if tags else "",
        "entry_link[badges]": "",
        "entry_link[image]": ("", "", "application/octet-stream"),
        "entry_link[imageUrl]": "",
        "entry_link[imageAlt]": "",
        "entry_link[lang]": KBOT_LANG,
        "entry_link[submit]": "",
        "entry_link[_token]": _csrf_token
    }

    #dictionary = string.ascii_letters + string.digits
    m = MultipartEncoder(
        fields=form_data
        #, boundary=f"----WebKitFormBoundary{''.join([random.choice(dictionary) for _ in range(16)])}"
    )

    headers = {
        # "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        # "Accept-Encoding": "gzip, deflate, br",
        # "Accept-Language": "en-US,en;q=0.9",
        # "Cache-Control": "max-age=0",
        "Content-Type": m.content_type,
        "Origin": f"https://{KBOT_INSTANCE}",
        "Referer": f"https://{KBOT_INSTANCE}/m/{KBOT_MAGAZINE}/new",
        # "Sec-Ch-Ua": '"Not.A/Brand";v="8", "Chromium";v="114", "Google Chrome";v="114"',
        # "Sec-Ch-Ua-Mobile": "?0",
        # "Sec-Ch-Ua-Platform": "Windows",
        # "Sec-Fetch-Dest": "document",
        # "Sec-Fetch-Mode": "navigate",
        # "Sec-Fetch-Site": "same-origin",
        # "Sec-Fetch-User": "?1",
        # "Upgrade-Insecure-Requests": "1",
        # "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36"
    }

    data = m.to_string().strip(b"\r\n")
    #sleep(1)
    retries = 3
    status = 422
    while status == 422 and retries > 0:
        response = kbin_session.post(f"https://{KBOT_INSTANCE}/m/{KBOT_MAGAZINE}/new", data=data, headers=headers)
        status = response.status_code
        if status == 422:
            retries -= 1
            logger.debug(f"Auto retrying after delay due to 422 error... ({retries} left)")
            sleep(2)
    #response = kbin_session.post(f"https://httpbin.org/post", files=file_data, data=form_data)
    #print(response.text)
    #sys.exit(1)
    if(response.status_code not in [200, 302]):
        logger.error(f"Unexpected status code: {response.status_code} - {response.url}")
        return False

    return True


def main():
    global logged_in
    while True:
        try:
            try:
                with open(cache_name) as f:
                    last_updated = parse(f.read())
            except FileNotFoundError:
                last_updated = parse("1970-01-01T00:00:00+00:00")
            response = requests.get(KBOT_RSS)
            if not (200 <= response.status_code < 300):
                sleep(KBOT_FREQUENCY)
                continue

            rss_data = RSSParser.parse(response.text)
            logger.debug(json.dumps(json.loads(rss_data.json()), indent=4))

            if not logged_in:
                login()

            logger.debug(rss_data.channel.title)

            for item in reversed(rss_data.channel.items):
                logger.debug(item.title)
                pub_date = parse(str(item.pub_date))
                logger.debug(pub_date)
                logger.debug(item.link)

                author = str(item.author)
                match = re.search("invalid\@example\.com \((.+)\)", author)
                if(match):
                    author = match.group(1)

                result = False

                if(last_updated < pub_date):
                    link = str(item.link)
                    title = f"{rss_data.channel.title} - {item.title}"
                    description = f"Author: {author}\n\nDescription: {rss_data.channel.description}"
                    try:
                        logger.debug(f"Posting '{title}'...")
                        result = post_link(link, title, description)

                        if not result:
                            logger.error("Post Failed! Attempting to login and post again...")
                            result = login() and post_link(link, title, description)
                        
                        if result:
                            logger.info(f"Successfully posted '{title}'")
                        else:
                            logger.error("Failed on retry =/")
                        
                    except Exception as e:
                        logger.error(f"Got exception while posting link: {e}")
        
            if result:
                if os.path.exists(cache_name):
                    shutil.copyfile(cache_name, f"{cache_name}.bak")

                try:
                    with open(cache_name, "w") as f:
                        f.write(datetime.utcnow().replace(tzinfo=tzutc()).isoformat())
                except Exception as e:
                    logger.error(f"Got exception while writing access time: {e}")
                    if os.path.exists(f"{cache_name}.bak"):
                        shutil.copyfile(f"{cache_name}.bak", cache_name)
                finally:
                    if os.path.exists(f"{cache_name}.bak"):
                        os.remove(f"{cache_name}.bak")

            sleep(KBOT_FREQUENCY)
        except KeyboardInterrupt:
            logger.info("Shutting down...")
            break
        except Exception as e:
            logger.error("Unhandled Error:", e)
            sleep(KBOT_FREQUENCY)

if __name__ == "__main__":
    main()