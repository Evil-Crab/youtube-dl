# coding: utf-8
from __future__ import unicode_literals

import base64
import json
import hashlib
import hmac
import random
import string
import time

from .common import InfoExtractor
from ..compat import (
    compat_HTTPError,
    compat_urllib_parse_urlencode,
    compat_urllib_parse,
)
from ..utils import (
    ExtractorError,
    float_or_none,
    int_or_none,
    url_or_none,
    try_get
)


class VRVBaseIE(InfoExtractor):
    _API_DOMAIN = None
    _API_PARAMS = {}
    _CMS_SIGNING = {}
    _TOKEN = None
    _TOKEN_SECRET = ''

    def _call_api(self, path, video_id, note, data=None):
        # https://tools.ietf.org/html/rfc5849#section-3
        base_url = self._API_DOMAIN + '/core/' + path
        query = [
            ('oauth_consumer_key', self._API_PARAMS['oAuthKey']),
            ('oauth_nonce', ''.join([random.choice(string.ascii_letters) for _ in range(32)])),
            ('oauth_signature_method', 'HMAC-SHA1'),
            ('oauth_timestamp', int(time.time())),
        ]
        if self._TOKEN:
            query.append(('oauth_token', self._TOKEN))
        encoded_query = compat_urllib_parse_urlencode(query)
        headers = self.geo_verification_headers()
        if data:
            data = json.dumps(data).encode()
            headers['Content-Type'] = 'application/json'
        base_string = '&'.join([
            'POST' if data else 'GET',
            compat_urllib_parse.quote(base_url, ''),
            compat_urllib_parse.quote(encoded_query, '')])
        oauth_signature = base64.b64encode(hmac.new(
            (self._API_PARAMS['oAuthSecret'] + '&' + self._TOKEN_SECRET).encode('ascii'),
            base_string.encode(), hashlib.sha1).digest()).decode()
        encoded_query += '&oauth_signature=' + compat_urllib_parse.quote(oauth_signature, '')
        try:
            return self._download_json(
                '?'.join([base_url, encoded_query]), video_id,
                note='Downloading %s JSON metadata' % note, headers=headers, data=data)
        except ExtractorError as e:
            if isinstance(e.cause, compat_HTTPError) and e.cause.code == 401:
                raise ExtractorError(json.loads(e.cause.read().decode())['message'], expected=True)
            raise

    def _call_cms(self, path, video_id, note):
        if not self._CMS_SIGNING:
            self._CMS_SIGNING = self._call_api('index', video_id, 'CMS Signing')['cms_signing']
        return self._download_json(
            self._API_DOMAIN + path, video_id, query=self._CMS_SIGNING,
            note='Downloading %s JSON metadata' % note, headers=self.geo_verification_headers())

    def _get_cms_resource(self, resource_key, video_id):
        return self._call_api(
            'cms_resource', video_id, 'resource path', data={
                'resource_key': resource_key,
            })['__links__']['cms_resource']['href']

    def _real_initialize(self):
        webpage = self._download_webpage(
            'https://vrv.co/', None, headers=self.geo_verification_headers())
        self._API_PARAMS = self._parse_json(self._search_regex(
            [
                r'window\.__APP_CONFIG__\s*=\s*({.+?})(?:</script>|;)',
                r'window\.__APP_CONFIG__\s*=\s*({.+})'
            ], webpage, 'app config'), None)['cxApiParams']
        self._API_DOMAIN = self._API_PARAMS.get('apiDomain', 'https://api.vrv.co')


class VRVIE(VRVBaseIE):
    IE_NAME = 'vrv'
    _VALID_URL = r'https?://(?:www\.)?vrv\.co/watch/(?P<id>[A-Z0-9]+)'
    _TESTS = [{
        'url': 'https://vrv.co/watch/GR9PNZ396/Hidden-America-with-Jonah-Ray:BOSTON-WHERE-THE-PAST-IS-THE-PRESENT',
        'info_dict': {
            'id': 'GR9PNZ396',
            'ext': 'mp4',
            'title': 'BOSTON: WHERE THE PAST IS THE PRESENT',
            'description': 'md5:4ec8844ac262ca2df9e67c0983c6b83f',
            'uploader_id': 'seeso',
        },
        'params': {
            # m3u8 download
            'skip_download': True,
        },
    }]
    _NETRC_MACHINE = 'vrv'

    def _real_initialize(self):
        super(VRVIE, self)._real_initialize()

        email, password = self._get_login_info()
        if email is None:
            return

        token_credentials = self._call_api(
            'authenticate/by:credentials', None, 'Token Credentials', data={
                'email': email,
                'password': password,
            })
        self._TOKEN = token_credentials['oauth_token']
        self._TOKEN_SECRET = token_credentials['oauth_token_secret']

    def _extract_vrv_formats(self, url, video_id, stream_format, audio_lang, hardsub_lang):
        if not url or stream_format not in ('hls', 'dash'):
            return []
        assert audio_lang or hardsub_lang
        stream_id_list = []
        if audio_lang:
            stream_id_list.append('audio-%s' % audio_lang)
        if hardsub_lang:
            stream_id_list.append('hardsub-%s' % hardsub_lang)
        stream_id = '-'.join(stream_id_list)
        format_id = '%s-%s' % (stream_format, stream_id)
        if stream_format == 'hls':
            adaptive_formats = self._extract_m3u8_formats(
                url, video_id, 'mp4', m3u8_id=format_id,
                note='Downloading %s m3u8 information' % stream_id,
                fatal=False)
        elif stream_format == 'dash':
            adaptive_formats = self._extract_mpd_formats(
                url, video_id, mpd_id=format_id,
                note='Downloading %s MPD information' % stream_id,
                fatal=False)
        if audio_lang:
            for f in adaptive_formats:
                if f.get('acodec') != 'none':
                    f['language'] = audio_lang
        return adaptive_formats

    def _real_extract(self, url):
        video_id = self._match_id(url)

        episode_path = self._get_cms_resource(
            'cms:/episodes/' + video_id, video_id)
        video_data = self._call_cms(episode_path, video_id, 'video')
        title = video_data['title']

        streams_path = video_data['__links__'].get('streams', {}).get('href')
        if not streams_path:
            self.raise_login_required()
        streams_json = self._call_cms(streams_path, video_id, 'streams')

        audio_locale = streams_json.get('audio_locale')
        formats = []
        for stream_type, streams in streams_json.get('streams', {}).items():
            if stream_type in ('adaptive_hls', 'adaptive_dash'):
                for stream in streams.values():
                    formats.extend(self._extract_vrv_formats(
                        stream.get('url'), video_id, stream_type.split('_')[1],
                        audio_locale, stream.get('hardsub_locale')))
        self._sort_formats(formats)

        subtitles = {}
        for subtitle in streams_json.get('subtitles', {}).values():
            subtitle_url = subtitle.get('url')
            if not subtitle_url:
                continue
            subtitles.setdefault(subtitle.get('locale', 'en-US'), []).append({
                'url': subtitle_url,
                'ext': subtitle.get('format', 'ass'),
            })

        thumbnails = []
        thumbnails_list = try_get(video_data, lambda x: x['images']['thumbnail'][0], list)
        if thumbnails_list:
            for thumbnail in thumbnails_list:
                thumbnail_url = url_or_none(thumbnail.get('source'))
                if not thumbnail_url:
                    continue
                thumbnails.append({
                    'url': thumbnail_url,
                    'width': int_or_none(thumbnail.get('width')),
                    'height': int_or_none(thumbnail.get('height')),
                })

        return {
            'id': video_id,
            'title': title,
            'formats': formats,
            'subtitles': subtitles,
            'thumbnails': thumbnails,
            'description': video_data.get('description'),
            'duration': float_or_none(video_data.get('duration_ms'), 1000),
            'uploader_id': video_data.get('channel_id'),
            'series': video_data.get('series_title'),
            'season': video_data.get('season_title'),
            'season_number': int_or_none(video_data.get('season_number')),
            'season_id': video_data.get('season_id'),
            'episode': title,
            'episode_number': int_or_none(video_data.get('episode_number')),
            'episode_id': video_data.get('production_episode_id'),
        }


class VRVSeriesIE(VRVBaseIE):
    IE_NAME = 'vrv:series'
    _VALID_URL = r'https?://(?:www\.)?vrv\.co/series/(?P<id>[A-Z0-9]+)'
    _TEST = {
        'url': 'https://vrv.co/series/G68VXG3G6/The-Perfect-Insider',
        'info_dict': {
            'id': 'G68VXG3G6',
        },
        'playlist_mincount': 11,
    }

    def _real_extract(self, url):
        series_id = self._match_id(url)

        seasons_path = self._get_cms_resource(
            'cms:/seasons?series_id=' + series_id, series_id)
        seasons_data = self._call_cms(seasons_path, series_id, 'seasons')

        entries = []
        for season in seasons_data.get('items', []):
            episodes_path = season['__links__']['season/episodes']['href']
            episodes = self._call_cms(episodes_path, series_id, 'episodes')
            for episode in episodes.get('items', []):
                episode_id = episode['id']
                entries.append(self.url_result(
                    'https://vrv.co/watch/' + episode_id,
                    'VRV', episode_id, episode.get('title')))

        return self.playlist_result(entries, series_id)
