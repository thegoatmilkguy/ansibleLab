#!/usr/bin/python
# -*- coding: utf-8 -*-
# Copyright (c) 2026 Chad Feeser
# MIT License (see LICENSE)

from __future__ import absolute_import, division, print_function
__metaclass__ = type

DOCUMENTATION = r'''
---
module: nasa_images
short_description: Search the NASA Image and Video Library and download images
version_added: "1.0.0"
description:
  - Searches the public NASA Image and Video Library (C(images-api.nasa.gov)) and saves the
    matching images into a directory on the target host.
  - No API key is required.
  - Only still images are returned. Videos and audio are never searched.
  - Files that already exist in O(dest) are left alone unless O(overwrite=true), so
    running the module again is safe.
options:
  dest:
    description:
      - Directory the images are saved into. It is created if it does not exist.
      - Each file is named C(<nasa_id>.<extension>). Characters other than letters, digits,
        C(.), C(_) and C(-) in the NASA ID are replaced with C(_).
      - The file options (O(mode), O(owner), O(group) and so on) apply to each image file,
        not to this directory.
    type: path
    required: true
  query:
    description:
      - Free-text search, for example C(nebula) or C(apollo 11).
      - Quote characters are not supported by the NASA service and a phrase in quotes finds nothing.
    type: str
  nasa_id:
    description:
      - Fetch exactly one item by its NASA ID, for example C(art002e021278).
      - Cannot be combined with O(query), O(center), O(year_start) or O(year_end).
    type: str
  center:
    description:
      - Limit results to one NASA center, for example C(JPL), C(GSFC) or C(JSC).
    type: str
  year_start:
    description:
      - First year to include (the year the item was created).
      - Must be between 1920 and the current year.
    type: int
  year_end:
    description:
      - Last year to include.
      - Must be between 1920 and the current year, and not earlier than O(year_start).
    type: int
  count:
    description:
      - How many images to save, from 1 to 25.
      - If fewer images match, the ones that do are saved and a warning says so.
    type: int
    default: 1
  shuffle:
    description:
      - Pick the images at random from the first 100 matches.
      - When false, the first O(count) matches in the order NASA returns them are used.
    type: bool
    default: false
  full_size:
    description:
      - Try the full-size original first. About half of the items have no original, and for
        those the normal listed size is saved instead.
      - Originals can be several megabytes each.
    type: bool
    default: false
  overwrite:
    description:
      - Download again even when the file already exists.
    type: bool
    default: false
extends_documentation_fragment:
  - files
attributes:
  check_mode:
    description: Can run in check_mode and return changed status prediction without modifying target.
    support: full
  diff_mode:
    description: Will return details on what has changed (or possibly needs changing in check_mode), when in diff mode.
    support: none
notes:
  - At least one of O(query), O(nasa_id), O(center), O(year_start) or O(year_end) is required.
  - The NASA service only returns the first 10000 matches of any search.
  - Only C(https) links on C(nasa.gov) hosts are downloaded. Results that point anywhere else are skipped.
  - A single file larger than 50 MB is refused.
  - Image files come straight from NASA. Check the usage guidelines at
    U(https://www.nasa.gov/nasa-brand-center/images-and-media/) before reusing them.
author:
  - Chad Feeser (@csfeeser)
'''

EXAMPLES = r'''
- name: Save one picture of a nebula
  alta3.nasa_images.nasa_images:
    dest: ~/images
    query: nebula

- name: Save three images made by JPL between 2020 and 2022
  alta3.nasa_images.nasa_images:
    dest: ~/images
    query: jupiter
    center: JPL
    year_start: 2020
    year_end: 2022
    count: 3

- name: Save one specific image by its NASA ID
  alta3.nasa_images.nasa_images:
    dest: ~/images
    nasa_id: art002e021278
  register: earthset

- name: Five random moon pictures at full size
  alta3.nasa_images.nasa_images:
    dest: /tmp/moon
    query: moon
    count: 5
    shuffle: true
    full_size: true

- name: Show what was saved
  ansible.builtin.debug:
    msg: "{{ item.title }} -> {{ item.path }}"
  loop: "{{ earthset.images }}"
'''

RETURN = r'''
images:
  description:
    - One entry per image, in the order they were saved.
    - If a download fails part way, the images saved before the failure are listed.
  returned: always
  type: list
  elements: dict
  contains:
    path:
      description: Where the file is (or would be, in check mode) on the target host.
      type: str
      sample: /home/student/images/art002e021278.jpg
    title:
      description: Title given by NASA.
      type: str
      sample: A Breathtaking Earthset from Orion
    date:
      description: Date the item was created, as YYYY-MM-DD.
      type: str
      sample: "2026-04-06"
    nasa_id:
      description: The NASA ID of the item.
      type: str
      sample: art002e021278
    description:
      description: Caption text from NASA. Can be empty.
      type: str
    center:
      description: NASA center that published the item. Can be empty.
      type: str
      sample: JSC
    url:
      description: The address the image was downloaded from.
      type: str
    downloaded:
      description: V(true) when the file was written, V(false) when it already existed and was kept.
      type: bool
total_hits:
  description: How many images matched the search in all (not only the ones saved).
  returned: when the search succeeded
  type: int
  sample: 1
'''

import datetime
import json
import os
import random
import re
import tempfile
import time
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils.common.text.converters import to_text
from ansible.module_utils.urls import fetch_url

SEARCH_URL = 'https://images-api.nasa.gov/search'
USER_AGENT = 'alta3-nasa_images-ansible/1.0'
TIMEOUT = 30
MIN_YEAR = 1920
MAX_COUNT = 25
SHUFFLE_POOL = 100
# Ask for a few more results than needed, so skipped entries do not leave us short.
SPARE_RESULTS = 10
MAX_BYTES = 50 * 1024 * 1024
IMAGE_EXTENSIONS = ('.jpg', '.jpeg', '.png')


def api_message(info):
    """A short, safe description of a failed request. Never includes the URL."""
    status = info.get('status')
    if status is None or status < 0:
        return 'could not reach NASA (%s)' % info.get('msg', 'unknown error')
    reason = ''
    body = info.get('body')
    if body:
        try:
            reason = json.loads(to_text(body)).get('reason', '')
        except (ValueError, AttributeError):
            reason = ''
    return 'NASA answered HTTP %s%s' % (status, ': %s' % reason if reason else '')


def request(module, url):
    """GET a URL, retrying once if NASA says to slow down (HTTP 429)."""
    for attempt in (1, 2):
        resp, info = fetch_url(module, url, headers={'User-Agent': USER_AGENT},
                               method='GET', timeout=TIMEOUT)
        if info.get('status') == 429 and attempt == 1:
            try:
                wait = int(info.get('retry-after', 2))
            except (TypeError, ValueError):
                wait = 2
            time.sleep(max(1, min(wait, 10)))
            continue
        return resp, info


def validate(module):
    p = module.params
    this_year = datetime.date.today().year
    if not 1 <= p['count'] <= MAX_COUNT:
        module.fail_json(msg='count must be between 1 and %d, got %d' % (MAX_COUNT, p['count']))
    for name in ('year_start', 'year_end'):
        value = p[name]
        if value is not None and not MIN_YEAR <= value <= this_year:
            module.fail_json(msg='%s must be between %d and %d, got %d' % (name, MIN_YEAR, this_year, value))
    if p['year_start'] is not None and p['year_end'] is not None and p['year_start'] > p['year_end']:
        module.fail_json(msg='year_start (%d) is later than year_end (%d)' % (p['year_start'], p['year_end']))
    for name in ('query', 'nasa_id', 'center'):
        if p[name] is not None and not p[name].strip():
            module.fail_json(msg='%s must not be empty' % name)
    if p['query'] and '"' in p['query']:
        module.warn('NASA search does not support quoted phrases; remove the quotes or expect no results')


def build_search_url(params, page_size):
    pairs = [('media_type', 'image'), ('page_size', page_size)]
    for key, name in (('query', 'q'), ('nasa_id', 'nasa_id'), ('center', 'center'),
                      ('year_start', 'year_start'), ('year_end', 'year_end')):
        if params.get(key) is not None:
            pairs.append((name, params[key]))
    return '%s?%s' % (SEARCH_URL, urlencode(pairs, quote_via=quote))


def clean_link(href):
    """Return a safe https URL for an image link, or None if it should not be fetched.

    Only plain nasa.gov hosts are allowed (no user info, no port). http is upgraded to
    https, and the path is percent-encoded because NASA links can contain raw spaces.
    """
    parts = urlsplit(href or '')
    host = (parts.hostname or '').lower()
    if parts.scheme not in ('http', 'https') or parts.netloc.lower() != host:
        return None
    if host != 'nasa.gov' and not host.endswith('.nasa.gov'):
        return None
    return urlunsplit(parts._replace(scheme='https', path=quote(parts.path, safe='/~%')))


def full_size_url(url):
    return re.sub(r'~\w+(\.\w+)$', r'~orig\1', url)


def file_name(nasa_id, url):
    stem = re.sub(r'[^A-Za-z0-9._-]+', '_', nasa_id).strip('._') or 'image'
    ext = os.path.splitext(urlsplit(url).path)[1].lower()
    if ext not in IMAGE_EXTENSIONS:
        ext = '.jpg'
    return stem + ext


def parse_items(payload):
    """Turn NASA's nested response into a flat list.

    Drops entries with no usable image link, and entries whose file name would clash
    with an earlier one (so two results never fight over the same file).
    """
    found = []
    taken = set()
    items = (payload.get('collection') or {}).get('items') or []
    for item in items:
        data = (item.get('data') or [{}])[0]
        raw = next((link.get('href') for link in (item.get('links') or [])
                    if (link.get('href') or '').lower().endswith(IMAGE_EXTENSIONS)), None)
        href = clean_link(raw)
        if not href or not data.get('nasa_id'):
            continue
        name = file_name(data['nasa_id'], href)
        if name in taken:
            continue
        taken.add(name)
        found.append({
            'nasa_id': data['nasa_id'],
            'title': data.get('title') or '',
            'date': (data.get('date_created') or '')[:10],
            'description': data.get('description') or '',
            'center': data.get('center') or '',
            'href': href,
            'file': name,
        })
    return found


def save(module, url, target):
    """Download url into target. Returns None on success, or an error message."""
    resp, info = request(module, url)
    if info.get('status') != 200 or resp is None:
        return api_message(info)
    try:
        if int(info.get('content-length') or 0) > MAX_BYTES:
            return 'the file is larger than %d MB' % (MAX_BYTES // (1024 * 1024))
    except ValueError:
        pass
    fd, tmp = tempfile.mkstemp(dir=module.tmpdir, prefix='nasa_images_')
    try:
        written = 0
        with os.fdopen(fd, 'wb') as handle:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_BYTES:
                    raise ValueError('the file is larger than %d MB' % (MAX_BYTES // (1024 * 1024)))
                handle.write(chunk)
        module.atomic_move(tmp, target, unsafe_writes=module.params.get('unsafe_writes', False))
    except Exception as exc:  # pylint: disable=broad-except
        if os.path.exists(tmp):
            os.unlink(tmp)
        return 'could not save the file: %s' % to_text(exc)
    return None


def download(module, url, target, full_size):
    """Save one image. Returns (the URL actually used, error message or None)."""
    if full_size:
        original = full_size_url(url)
        if original != url and save(module, original, target) is None:
            return original, None
    return url, save(module, url, target)


def main():
    module = AnsibleModule(
        argument_spec=dict(
            dest=dict(type='path', required=True),
            query=dict(type='str'),
            nasa_id=dict(type='str'),
            center=dict(type='str'),
            year_start=dict(type='int'),
            year_end=dict(type='int'),
            count=dict(type='int', default=1),
            shuffle=dict(type='bool', default=False),
            full_size=dict(type='bool', default=False),
            overwrite=dict(type='bool', default=False),
        ),
        required_one_of=[('query', 'nasa_id', 'center', 'year_start', 'year_end')],
        mutually_exclusive=[('nasa_id', 'query'), ('nasa_id', 'center'),
                            ('nasa_id', 'year_start'), ('nasa_id', 'year_end')],
        add_file_common_args=True,
        supports_check_mode=True,
    )
    validate(module)
    p = module.params
    dest = p['dest']

    if os.path.exists(dest) and not os.path.isdir(dest):
        module.fail_json(msg='dest %s exists and is not a directory' % dest, images=[])

    if p['nasa_id']:
        page_size = 1
    elif p['shuffle']:
        page_size = SHUFFLE_POOL
    else:
        page_size = min(SHUFFLE_POOL, p['count'] + SPARE_RESULTS)
    resp, info = request(module, build_search_url(p, page_size))
    if info.get('status') != 200 or resp is None:
        module.fail_json(msg='Search failed: %s' % api_message(info), images=[])
    try:
        payload = json.loads(to_text(resp.read()))
    except ValueError:
        module.fail_json(msg='Search failed: NASA returned something that is not JSON', images=[])

    total_hits = ((payload.get('collection') or {}).get('metadata') or {}).get('total_hits', 0)
    candidates = parse_items(payload)
    if not candidates:
        module.fail_json(msg='No images found for that search. Try a broader query or a wider year range.',
                         images=[], total_hits=total_hits)
    if p['shuffle'] and len(candidates) > p['count']:
        candidates = random.sample(candidates, p['count'])
    candidates = candidates[:p['count']]
    if len(candidates) < p['count']:
        module.warn('Asked for %d images but only %d matched; saving %d.'
                    % (p['count'], len(candidates), len(candidates)))

    changed = False
    images = []
    if not os.path.isdir(dest):
        changed = True
        if not module.check_mode:
            try:
                os.makedirs(dest)
            except OSError as exc:
                module.fail_json(msg='Could not create dest %s: %s' % (dest, to_text(exc)), images=[])

    for item in candidates:
        target = os.path.join(dest, item['file'])
        used_url = item['href']
        downloaded = False
        if p['overwrite'] or not os.path.exists(target):
            downloaded = True
            if not module.check_mode:
                used_url, error = download(module, item['href'], target, p['full_size'])
                if error:
                    module.fail_json(msg='Could not download %s: %s' % (item['nasa_id'], error),
                                     changed=changed, images=images, total_hits=total_hits)
            changed = True
        if os.path.exists(target):
            file_args = module.load_file_common_arguments(p, path=target)
            changed = module.set_fs_attributes_if_different(file_args, changed)
        images.append({
            'path': target,
            'title': item['title'],
            'date': item['date'],
            'nasa_id': item['nasa_id'],
            'description': item['description'],
            'center': item['center'],
            'url': used_url,
            'downloaded': downloaded,
        })

    module.exit_json(changed=changed, images=images, total_hits=total_hits)


if __name__ == '__main__':
    main()
