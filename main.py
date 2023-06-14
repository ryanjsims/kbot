from requests_toolbelt import MultipartEncoder
import requests
import os
import re
import json
import shutil
from time import sleep, time
from datetime import datetime, timedelta
from dateutil.parser import *
from dateutil.tz import tzutc
from dotenv import load_dotenv
from rss_parser import Parser as RSSParser
from typing import Optional, List, Dict, Union
import logging

logging.basicConfig()
logger = logging.getLogger("kbot")
logger.setLevel(logging.DEBUG)

load_dotenv()

TOKEN_REGEX = re.compile('"(_csrf_token|entry_link\[_token\]|entry_comment\[_token\])"\s+value="(.+)"')
MAGAZINE_REGEX = re.compile('"entry_link\[magazine\]\[autocomplete\]".+value="([0-9]+)"\sselected="selected"')
# Group 1 is thread id, 2 is title
THREAD_REGEX = re.compile('id="entry-([0-9]+)"[\sa-zA-Z\-=":@>#<0-9]+<a\s+href=".+">(.+)<\/a>')

KBOT_USER = os.getenv("KBOT_USER")
KBOT_PASS = os.getenv("KBOT_PASS")
KBOT_INSTANCE = os.getenv("KBOT_INSTANCE")
KBOT_MAGAZINE = os.getenv("KBOT_MAGAZINE")
KBOT_RSS = os.getenv("KBOT_RSS")
KBOT_LANG = os.getenv("KBOT_LANG")

KBOT_FREQUENCY = max(120, int(os.getenv("KBOT_FREQUENCY", "600")))
KBOT_THREAD_CACHE_SECONDS = max(10, int(os.getenv("KBOT_THREAD_CACHE_SECONDS", "30")))

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

    m = MultipartEncoder(
        fields=form_data
    )

    headers = {
        "Content-Type": m.content_type,
        "Origin": f"https://{KBOT_INSTANCE}",
        "Referer": f"https://{KBOT_INSTANCE}/m/{KBOT_MAGAZINE}/new"
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
    
    if(response.status_code not in [200, 302]):
        logger.error(f"Unexpected status code: {response.status_code} - {response.url}")
        return False

    return True

###
# Dictionary of magazine names to dictionaries containing two keys:
#    - "cached_at" -> datetime
#    - "threads" -> Dict[int, str]
#
cached_threads: Dict[str, Dict[str, Union[datetime, Dict[int, str]]]] = {}
THREAD_CACHE_TIMEOUT = timedelta(seconds=KBOT_THREAD_CACHE_SECONDS)

# Lists threads in magazine by id -> title
# Caches threads automatically for 10 to infinite seconds, configurable with .env KBOT_THREAD_CACHE_SECONDS
def list_threads(magazine: str) -> Dict[int, str]:
    global cached_threads
    if magazine in cached_threads and (datetime.utcnow() - cached_threads[magazine]["cached_at"]) < THREAD_CACHE_TIMEOUT:
        return cached_threads[magazine]["threads"]
    to_return = {}
    response = kbin_session.get(f"https://{KBOT_INSTANCE}/m/{magazine}")
    if response.status_code != 200:
        logger.error(f"Got unexpected status while retrieving threads: {response.status_code}")
        return to_return
    matches: List[re.Match[str]] = THREAD_REGEX.findall(response.text)
    for match in matches:
        thread_id = int(match.group(1))
        title = match.group(2)
        to_return[thread_id] = title

    cached_threads[magazine] = {
        "cached_at": datetime.utcnow(),
        "threads": to_return
    }
    return to_return

def post_toplevel_comment(magazine: str, thread_id: int, body: str, lang: str) -> bool:
    response = kbin_session.get(f"https://{KBOT_INSTANCE}/m/{magazine}/t/{thread_id}")
    if response.status_code != 200:
        logger.error(f"Unexpected status code while retrieving thread: {response.status_code}")
        return False
    csrf_token = get_csrf(response)

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