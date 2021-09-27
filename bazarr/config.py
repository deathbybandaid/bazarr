# coding=utf-8
# pylama:ignore=W0611
# TODO unignore and fix W0611

import hashlib
import os
import ast

from urllib.parse import quote_plus

from subliminal.cache import region

from simpleconfigparser import simpleconfigparser, configparser, NoOptionError

from get_args import args


class SimpleConfigParser(simpleconfigparser):

    def get(self, section, option, raw=False, vars=None):
        try:
            return configparser.get(self, section, option, raw=raw, vars=vars)
        except NoOptionError:
            return None


defaults = {
    'general': {
        'ip': '0.0.0.0',
        'port': '6767',
        'base_url': '',
        'debug': 'False',
        'branch': 'master',
        'auto_update': 'True',
        'single_language': 'False',
        'minimum_score': '90',
        'use_postprocessing': 'False',
        'postprocessing_cmd': '',
        'postprocessing_threshold': '90',
        'use_postprocessing_threshold': 'False',
        'postprocessing_threshold_movie': '70',
        'use_postprocessing_threshold_movie': 'False',
        'use_series': 'False',
        'use_movies': 'False',
        'serie_default_enabled': 'False',
        'serie_default_profile': '',
        'movie_default_enabled': 'False',
        'movie_default_profile': '',
        'page_size': '25',
        'page_size_manual_search': '10',
        'minimum_score_movie': '70',
        'use_embedded_subs': 'True',
        'embedded_subs_show_desired': 'True',
        'utf8_encode': 'True',
        'ignore_pgs_subs': 'False',
        'ignore_vobsub_subs': 'False',
        'ignore_ass_subs': 'False',
        'adaptive_searching': 'False',
        'enabled_providers': '[]',
        'multithreading': 'True',
        'chmod_enabled': 'False',
        'chmod': '0640',
        'subfolder': 'current',
        'subfolder_custom': '',
        'upgrade_subs': 'True',
        'upgrade_frequency': '12',
        'days_to_upgrade_subs': '7',
        'upgrade_manual': 'True',
        'anti_captcha_provider': 'None',
        'wanted_search_frequency': '3',
        'wanted_search_frequency_movie': '3',
        'subzero_mods': '[]',
        'dont_notify_manual_actions': 'False',
        'filewatcher_timeout': '60',  # file watcher interval in seconds
        'filewatcher_type': 'local'  # file watcher mode: local or remote
    },
    'auth': {
        'type': 'None',
        'username': '',
        'password': ''
    },
    'series': {
        'full_update': 'Daily',
        'full_update_day': '6',
        'full_update_hour': '4',
        'only_monitored': 'False',
        'excluded_tags': '[]',
        'excluded_series_types': '[]',
        'use_ffprobe_cache': 'True'
    },
    'movies': {
        'full_update': 'Daily',
        'full_update_day': '6',
        'full_update_hour': '5',
        'only_monitored': 'False',
        'excluded_tags': '[]',
        'use_ffprobe_cache': 'True'
    },
    'proxy': {
        'type': 'None',
        'url': '',
        'port': '',
        'username': '',
        'password': '',
        'exclude': '["localhost","127.0.0.1"]'
    },
    'anticaptcha': {
        'anti_captcha_key': ''
    },
    'deathbycaptcha': {
        'username': '',
        'password': ''
    },
    'analytics': {
        'enabled': 'True'
    },
    'subsync': {
        'use_subsync': 'False',
        'use_subsync_threshold': 'False',
        'subsync_threshold': '90',
        'use_subsync_movie_threshold': 'False',
        'subsync_movie_threshold': '70',
        'debug': 'False'
    },
    'series_scores': {
        "hash": 359,
        "series": 180,
        "year": 90,
        "season": 30,
        "episode": 30,
        "release_group": 15,
        "source": 7,
        "audio_codec": 3,
        "resolution": 2,
        "video_codec": 2,
        "hearing_impaired": 1,
        "streaming_service": 0,
        "edition": 0,
    },
    'movie_scores': {
        "hash": 119,
        "title": 60,
        "year": 30,
        "release_group": 15,
        "source": 7,
        "audio_codec": 3,
        "resolution": 2,
        "video_codec": 2,
        "hearing_impaired": 1,
        "streaming_service": 0,
        "edition": 0,
    },
    'opensubtitles': {
        'username': '',
        'password': '',
        'use_tag_search': 'False',
        'vip': 'False',
        'ssl': 'False',
        'timeout': '15',
        'skip_wrong_fps': 'False'
    },
    'opensubtitlescom': {
        'username': '',
        'password': '',
        'use_hash': 'True'
    },
    'addic7ed': {
        'username': '',
        'password': ''
    },
    'legendasdivx': {
        'username': '',
        'password': '',
        'skip_wrong_fps': 'False'
    },
    'ktuvit': {
        'email': '',
        'hashed_password': ''
    },
    'legendastv': {
        'username': '',
        'password': '',
        'featured_only': 'False'
    },
    'xsubs': {
        'username': '',
        'password': ''
    },
    'assrt': {
        'token': ''
    },
    'napisy24': {
        'username': '',
        'password': ''
    },
    'subscene': {
        'username': '',
        'password': ''
    },
    'betaseries': {
        'token': ''
    },
    'titlovi': {
        'username': '',
        'password': ''
    },
    'titulky': {
        'username': '',
        'password': ''
    }
}

settings = SimpleConfigParser(defaults=defaults, interpolation=None)
settings.read(os.path.join(args.config_dir, 'config', 'config.ini'))

settings.general.base_url = settings.general.base_url if settings.general.base_url else '/'
base_url = settings.general.base_url.rstrip('/')

ignore_keys = ['flask_secret_key',
               'page_size',
               'page_size_manual_search',
               'throtteled_providers']

raw_keys = ['movie_default_forced', 'serie_default_forced']

array_keys = ['excluded_tags',
              'exclude',
              'subzero_mods',
              'excluded_series_types',
              'enabled_providers']

str_keys = ['chmod']

empty_values = ['', 'None', 'null', 'undefined', None, []]

if os.path.exists(os.path.join(args.config_dir, 'config', 'config.ini')):
    with open(os.path.join(args.config_dir, 'config', 'config.ini'), 'w+') as handle:
        settings.write(handle)


def get_settings():
    result = dict()
    sections = settings.sections()

    for sec in sections:
        sec_values = settings.items(sec, False)
        values_dict = dict()

        for sec_val in sec_values:
            key = sec_val[0]
            value = sec_val[1]

            if key in ignore_keys:
                continue

            if key not in raw_keys:
                # Do some postprocessings
                if value in empty_values:
                    if key in array_keys:
                        value = []
                    else:
                        continue
                elif key in array_keys:
                    value = get_array_from(value)
                elif value == 'True':
                    value = True
                elif value == 'False':
                    value = False
                else:
                    if key not in str_keys:
                        try:
                            value = int(value)
                        except ValueError:
                            pass

            values_dict[key] = value

        result[sec] = values_dict

    return result


def save_settings(settings_items):
    from database import database

    configure_debug = False
    configure_captcha = False
    update_schedule = False
    # update_path_map = False
    # TODO W0612 local variable 'update_path_map' is assigned to but never used
    configure_proxy = False
    exclusion_updated = False
    series_exclusion_updated = False
    movies_exclusion_updated = False

    # Subzero Mods
    update_subzero = False
    subzero_mods = get_array_from(settings.general.subzero_mods)

    if len(subzero_mods) == 1 and subzero_mods[0] == '':
        subzero_mods = []

    for key, value in settings_items:

        settings_keys = key.split('-')

        # Make sure that text based form values aren't pass as list
        if isinstance(value, list) and len(value) == 1 and settings_keys[-1] not in array_keys:
            value = value[0]
            if value in empty_values and value != '':
                value = None

        # Make sure empty language list are stored correctly
        if settings_keys[-1] in array_keys and value[0] in empty_values:
            value = []

        if value == 'true':
            value = 'True'
        elif value == 'false':
            value = 'False'

        if key == 'settings-auth-password':
            if value != settings.auth.password and value is not None:
                value = hashlib.md5(value.encode('utf-8')).hexdigest()

        if key == 'settings-general-debug':
            configure_debug = True

        if key in ['settings-general-anti_captcha_provider', 'settings-anticaptcha-anti_captcha_key',
                   'settings-deathbycaptcha-username', 'settings-deathbycaptcha-password']:
            configure_captcha = True

        if key in ['update_schedule', 'settings-general-auto_update', 'settings-general-upgrade_subs',
                   'settings-general-wanted_search_frequency', 'settings-general-wanted_search_frequency_movie',
                   'settings-general-upgrade_frequency']:
            update_schedule = True

        if key in ['settings-proxy-type', 'settings-proxy-url', 'settings-proxy-port', 'settings-proxy-username',
                   'settings-proxy-password']:
            configure_proxy = True

        if key in ['settings-series-excluded_tags', 'settings-series-only_monitored',
                   'settings-series-excluded_series_types', 'settings.movies.excluded_tags',
                   'settings-movies-only_monitored']:
            exclusion_updated = True

        if key in ['settings-series-excluded_tags', 'settings-series-only_monitored',
                   'settings-series-excluded_series_types']:
            series_exclusion_updated = True

        if key in ['settings-movies-excluded_tags', 'settings-movies-only_monitored']:
            movies_exclusion_updated = True

        if key == 'settings-addic7ed-username':
            if key != settings.addic7ed.username:
                region.delete('addic7ed_data')

        if key == 'settings-legendasdivx-username':
            if key != settings.legendasdivx.username:
                region.delete('legendasdivx_cookies2')

        if key == 'settings-opensubtitles-username':
            if key != settings.opensubtitles.username:
                region.delete('os_token')

        if key == 'settings-opensubtitlescom-username':
            if key != settings.opensubtitlescom.username:
                region.delete('oscom_token')

        if key == 'settings-subscene-username':
            if key != settings.subscene.username:
                region.delete('subscene_cookies2')

        if key == 'settings-titlovi-username':
            if key != settings.titlovi.username:
                region.delete('titlovi_token')

        if settings_keys[0] == 'settings':
            settings[settings_keys[1]][settings_keys[2]] = str(value)

        if settings_keys[0] == 'subzero':
            mod = settings_keys[1]
            enabled = value == 'True'
            if mod in subzero_mods and not enabled:
                subzero_mods.remove(mod)
            elif enabled:
                subzero_mods.append(mod)

            # Handle color
            if mod == 'color':
                previous = None
                for exist_mod in subzero_mods:
                    if exist_mod.startswith('color'):
                        previous = exist_mod
                        break
                if previous is not None:
                    subzero_mods.remove(previous)
                if value not in empty_values:
                    subzero_mods.append(value)

            update_subzero = True

    if update_subzero:
        settings.set('general', 'subzero_mods', ','.join(subzero_mods))

    with open(os.path.join(args.config_dir, 'config', 'config.ini'), 'w+') as handle:
        settings.write(handle)

    # Reconfigure Bazarr to reflect changes
    if configure_debug:
        from logger import configure_logging
        configure_logging(settings.general.getboolean('debug') or args.debug)

    if configure_captcha:
        configure_captcha_func()

    if update_schedule:
        from api import scheduler
        scheduler.update_configurable_tasks()

    if configure_proxy:
        configure_proxy_func()

    if exclusion_updated:
        from event_handler import event_stream
        event_stream(type='badges')
        if series_exclusion_updated:
            event_stream(type='reset-episode-wanted')
        if movies_exclusion_updated:
            event_stream(type='reset-movie-wanted')


def get_array_from(property):
    if property:
        if '[' in property:
            return ast.literal_eval(property)
        elif ',' in property:
            return property.split(',')
        else:
            return [property]
    else:
        return []


def configure_captcha_func():
    # set anti-captcha provider and key
    if settings.general.anti_captcha_provider == 'anti-captcha' and settings.anticaptcha.anti_captcha_key != "":
        os.environ["ANTICAPTCHA_CLASS"] = 'AntiCaptchaProxyLess'
        os.environ["ANTICAPTCHA_ACCOUNT_KEY"] = str(settings.anticaptcha.anti_captcha_key)
    elif settings.general.anti_captcha_provider == 'death-by-captcha' and settings.deathbycaptcha.username != "" and \
            settings.deathbycaptcha.password != "":
        os.environ["ANTICAPTCHA_CLASS"] = 'DeathByCaptchaProxyLess'
        os.environ["ANTICAPTCHA_ACCOUNT_KEY"] = str(':'.join(
            {settings.deathbycaptcha.username, settings.deathbycaptcha.password}))
    else:
        os.environ["ANTICAPTCHA_CLASS"] = ''


def configure_proxy_func():
    if settings.proxy.type != 'None':
        if settings.proxy.username != '' and settings.proxy.password != '':
            proxy = settings.proxy.type + '://' + quote_plus(settings.proxy.username) + ':' + \
                    quote_plus(settings.proxy.password) + '@' + settings.proxy.url + ':' + settings.proxy.port
        else:
            proxy = settings.proxy.type + '://' + settings.proxy.url + ':' + settings.proxy.port
        os.environ['HTTP_PROXY'] = str(proxy)
        os.environ['HTTPS_PROXY'] = str(proxy)
        exclude = ','.join(get_array_from(settings.proxy.exclude))
        os.environ['NO_PROXY'] = exclude
