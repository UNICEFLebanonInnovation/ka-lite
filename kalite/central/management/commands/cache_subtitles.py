"""Download subtitles for every video we have in every language they are available
according to the mapping generated by generate_subtitle_map
"""

import datetime
import glob
import json
import os
import sys
import time
import zipfile
from functools import partial
from optparse import make_option

from django.core.management.base import BaseCommand, CommandError
from django.core.management import call_command

import settings
from generate_subtitle_map import SRTS_JSON_FILENAME, LANGUAGE_SRT_FILENAME, headers
from main.topicdata import LANGUAGE_LOOKUP, LANGUAGE_LIST
from settings import LOG as logging
from utils import general
from utils.subtitles import subtitle_utils


download_path = settings.STATIC_ROOT + "srt/"  # kalite/static/


class LanguageCodeDoesNotExist(Exception):

    def __init__(self, lang_code):
        self.lang_code = lang_code

    def __str__(self):
        return "The language code specified (%s) does not have any available subtitles for download." % (self.lang_code)


class LanguageNameDoesNotExist(Exception):

    def __init__(self, lang_code):
        self.lang_code = lang_code

    def __str__(self):
        return "The language name for (%s) doesn't exist yet. Please add it to the lookup dictionary located at static/data/languages.json" % self.lang_code


def download_srt_from_3rd_party(*args, **kwargs):
    """Download subtitles specified by command line args"""
    # json generated by download_subtitles.py (this file)
    srt_list_path = settings.SUBTITLES_DATA_ROOT + LANGUAGE_SRT_FILENAME
    if not os.path.exists(srt_list_path):
        raise CommandError(
            "%s not found. Please run the 'generate_subtitle_map' command before calling this command." % srt_list_path)

    language_srt_map = json.loads(open(srt_list_path).read())

    lang_code = kwargs.get("lang_code", None)

    # if language specified, do those, if not do all
    if lang_code:
        try:
            vids_in_language = language_srt_map[lang_code]
        except:
            raise LanguageCodeDoesNotExist(lang_code)
        download_if_criteria_met(vids_in_language, *args, **kwargs)

    else:
        for lang_code, videos in language_srt_map.iteritems():
            kwargs["lang_code"] = lang_code
            download_if_criteria_met(videos, *args, **kwargs)


def get_srt_path(download_path, lang_code):
    return download_path + lang_code + "/subtitles/"


def download_if_criteria_met(videos, lang_code, force, response_code, date_since_attempt, frequency_to_save, *args, **kwargs):
    """Execute download of subtitle if it meets the criteria specified by the command line args

    Note: videos are a dict; keys=youtube_id, values=data
    """

    date_specified = general.convert_date_input(date_since_attempt)

    # Filter up front, for efficiency (& reporting's sake)
    n_videos = len(videos)

    logging.info("There are (up to) %s total videos with subtitles for language '%s'.  Let's go get them!" % (n_videos, lang_code))

    # Filter based on response code
    if response_code and response_code != "all":
        logging.info("Filtering based on response code (%s)..." %
                     response_code)
        response_code_filter = partial(
            lambda vid, rcode: rcode == vid["api_response"], rcode=response_code)
        videos = dict([(k, v)
                      for k, v in videos.items() if response_code_filter(v)])
        logging.info("%4d of %4d videos match your specified response code (%s)" %
                     (len(videos), n_videos, response_code))

    if date_specified:
        logging.info("Filtering based on date...")
        date_filter = partial(lambda vid, dat: not vid["last_attempt"] or datetime.datetime.strptime(
            last_attempt, '%Y-%m-%d') < dat, date_specified)
        videos = dict([(k, v) for k, v in videos.items() if date_filter(v)])
        logging.info("%4d of %4d videos need refreshing (last refresh < %s)" %
                     (len(videos), n_videos, date_specified))

    # Loop over good videos
    n_loops = 0
    for youtube_id, entry in videos.items():
        previously_downloaded = entry.get("downloaded")

        if previously_downloaded and not force:
            logging.info("Already downloaded %s/%s. To redownload, run again with -R." %
                         (lang_code, youtube_id))
            continue

        logging.info("Attempting to download subtitle for lang: %s and YouTube ID: %s" %
                     (lang_code, youtube_id))
        response = download_subtitle(youtube_id, lang_code, format="srt")
        time_of_attempt = unicode(datetime.datetime.now().date())

        if response == "client-error" or response == "server-error":
            # Couldn't download
            logging.info("Updating JSON file to record %s." % response)
            update_json(
                youtube_id, lang_code, previously_downloaded, response, time_of_attempt)

        else:
            dirpath = get_srt_path(download_path, lang_code)
            filename = youtube_id + ".srt"
            fullpath = dirpath + filename
            logging.info("Writing file to %s" % fullpath)

            if not os.path.exists(dirpath):
                os.makedirs(dirpath)
            with open(fullpath, 'w') as fp:
                fp.write(response.encode('UTF-8'))

            logging.info("Updating JSON file to record success.")
            update_json(
                youtube_id, lang_code, True, "success", time_of_attempt)

        # Update srt availability mapping
        n_loops += 1
        if n_loops % frequency_to_save == 0:
            logging.info(
                "On loop %d - generating new subtitle counts & updating srt availability!" % n_loops)
            get_new_counts(data_path=settings.SUBTITLES_DATA_ROOT,
                           download_path=download_path, language_code=lang_code)
            update_srt_availability(lang_code=lang_code)

    # One last call, to make sure we didn't miss anything.
    srt_availability = update_srt_availability(lang_code=lang_code)

    # Summarize output
    logging.info("We now have %d subtitles (amara thought they had %d) for language '%s'!" % (len(srt_availability), n_videos, lang_code))


def download_subtitle(youtube_id, lang_code, format="srt"):
    """Return subtitles for YouTube ID in language specified. Return False if they do not exist. Update local JSON accordingly."""
    assert format == "srt", "We only support srt download at the moment."

    api_info_map = json.loads(
        open(settings.SUBTITLES_DATA_ROOT + SRTS_JSON_FILENAME).read())
    # get amara id
    amara_code = api_info_map.get(youtube_id).get("amara_code")

    # make request
    # Please see http://amara.readthedocs.org/en/latest/api.html
    base_url = "https://amara.org/api2/partners/videos"

    r = subtitle_utils.make_request(headers, "%s/%s/languages/%s/subtitles/?format=srt" % (
        base_url, amara_code, lang_code))
    if r:
        # return the subtitle text, replacing empty subtitle lines with
        # spaces to make the FLV player happy
        try:
            response = (r.text or "").replace(
                "\n\n\n", "\n   \n\n").replace("\r\n\r\n\r\n", "\r\n   \r\n\r\n")
        except:
            response = r
        return response
    return False


def update_json(youtube_id, lang_code, downloaded, api_response, time_of_attempt):
    """Update language_srt_map to reflect download status"""
    # Open JSON file
    filepath = settings.SUBTITLES_DATA_ROOT + LANGUAGE_SRT_FILENAME
    language_srt_map = json.loads(open(filepath).read())

    # create updated entry
    entry = language_srt_map[lang_code][youtube_id]
    entry["downloaded"] = downloaded
    entry["api_response"] = api_response
    entry["last_attempt"] = time_of_attempt
    if api_response == "success":
        entry["last_success"] = time_of_attempt

    # update full-size JSON with new information
    language_srt_map[lang_code][youtube_id].update(entry)

    # write it to file
    logging.info("File updated.")
    json_file = open(filepath, "a+")
    json_file.write(json.dumps(language_srt_map))
    json_file.close()


def generate_zipped_srts(lang_codes_to_update, download_path):

    # Create media directory if it doesn't yet exist
    general.ensure_dir(settings.MEDIA_ROOT)
    zip_path = settings.MEDIA_ROOT + "subtitles/"
    general.ensure_dir(zip_path)
    lang_codes_to_update = lang_codes_to_update or os.listdir(download_path)

    for lang_code in lang_codes_to_update:
        srt_dir = os.path.join(download_path, lang_code, "subtitles")
        zip_file = os.path.join(zip_path, "%s_subtitles.zip" % lang_code)

        # Remove any old version (as we may not re-create)
        if os.path.exists(zip_file):
            os.remove(zip_file)

        if not os.path.exists(srt_dir):
            logging.warn("No srt directory for %s; skipping." % lang_code)
            continue

        srts = glob.glob(os.path.join(srt_dir, "*.srt"))
        if len(srts) == 0:
            logging.warn("No srts for %s; skipping." % lang_code)
            continue

        logging.info("Zipping up a new pack for language code: %s" % lang_code)
        zf = zipfile.ZipFile(zip_file, 'w')
        for f in srts:
            zf.write(f, arcname=os.path.basename(f))
        zf.close()


def get_new_counts(data_path, download_path, language_code):
    """Return dictionary of srt file counts in respective download folders"""

    subtitle_counts = {}
    subtitles_path = "%s%s/subtitles/" % (download_path, language_code)
    lang_name = get_language_name(language_code)

    try:
        count = len(glob.glob("%s/*.srt" % subtitles_path))
        logging.info("%4d subtitles for %-20s" % (count, lang_name))

        subtitle_counts[lang_name] = {}
        subtitle_counts[lang_name]["count"] = count
        subtitle_counts[lang_name]["code"] = language_code
    except LanguageNameDoesNotExist as ldne:
        logging.warn(ldne)
    except:
        logging.info("%-4s subtitles for %-20s" % ("No", lang_name))

    write_new_json(subtitle_counts, data_path)
    update_language_list(subtitle_counts, data_path)

    return subtitle_counts


def get_language_name(lang_code):
    """Return full language name from ISO 639-1 language code, raise exception if it isn't hardcoded yet"""
    language_name = LANGUAGE_LOOKUP.get(lang_code)
    if language_name:
        # logging.info("%s: %s" %(lang_code, language_name))
        return language_name
    else:
        raise LanguageNameDoesNotExist(lang_code)


def write_new_json(subtitle_counts, data_path):
    """Write JSON to file in static/data/subtitles/"""
    filename = "subtitle_counts.json"
    filepath = data_path + filename
    logging.info("Writing fresh srt counts to %s" % filepath)
    with open(filepath, 'wb') as fp:
        json.dump(subtitle_counts, fp)


def update_language_list(sub_counts, data_path):
    """Update hardcoded language codes if any supported subtitle languages aren't there."""
    for data in sub_counts.values():
        lang_code = data.get("code")
        if lang_code not in LANGUAGE_LIST:
            logging.info("Adding %s to language code list" % lang_code)
            LANGUAGE_LIST.append(lang_code)
    with open(os.path.join(data_path, "listedlanguages.json"), 'wb') as fp:
        json.dump(LANGUAGE_LIST, fp)


def update_srt_availability(lang_code):
    """Update maps in srts_by_lanugage with ids of downloaded subs"""

    srts_path = settings.STATIC_ROOT + "srt/"

    # Get a list of all srt files
    lang_srts_path = srts_path + lang_code + "/subtitles/"
    if not os.path.exists(lang_srts_path):
        # this happens when we tried to get srts, but none existed.
        yt_ids = []
    else:
        files = os.listdir(lang_srts_path)
        yt_ids = [f.rstrip(".srt") for f in files]
    srts_dict = { "srt_files": yt_ids }

    # Dump that to the language path
    base_path = settings.SUBTITLES_DATA_ROOT + "languages/"
    general.ensure_dir(base_path)
    filename = "%s.json" % lang_code
    filepath = base_path + filename
    with open(filepath, 'wb') as fp:
        json.dump(srts_dict, fp)

    return yt_ids


class Command(BaseCommand):
    help = "Update the mapping of subtitles available by language for each video. Location: static/data/subtitles/srts_download_status.json"

    option_list = BaseCommand.option_list + (
        make_option('-l', '--language',
                    action='store',
                    dest='lang_code',
                    default=None,
                    metavar="LANG_CODE",
                    help="Specify a particular language code to download subtitles for. Can be used with -R to update previously downloaded subtitles."),
        make_option('-f', '--force',
                    action='store_true',
                    dest='force',
                    default=False,
                    metavar="FORCE",
                    help="Force re-downloading of previously downloaded subtitles to refresh the repo. Can be used with -l. Default behavior is to not re-download subtitles we already have."),
        make_option('-d', '--date_since_attempt',
                    action='store',
                    dest='date_since_attempt',
                    default=None,
                    metavar="DATE",
                    help="Setting a date flag will update only those entries which have not been attempted since that date. Can be combined with -r. This could potentially be useful for updating old subtitles. USAGE: '-d MM/DD/YYYY'."),
        make_option('-r', '--response-code',
                    action='store',
                    dest='response_code',
                    default="",
                    metavar="RESP_CODE",
                    help="Which api-response code to recheck. Can be combined with -d. USAGE: '-r success', '-r client-error',  '-r server-error', or '-r all'  Default: -r (empty)"),
        make_option('-s', '--frequency_to_save',
                    action='store',
                    dest='frequency_to_save',
                    default=5,
                    metavar="FREQ_SAVE",
                    help="How often to update the srt availability status. The script will go FREQ_SAVE loops before running update_srt_availability"),
    )

    def handle(self, *args, **options):
        try:
            lang_codes = [options["lang_code"]] if options[
                "lang_code"] else None

            logging.info("Downloading...")
            download_srt_from_3rd_party(**options)

            logging.info(
                "Executed successfully! Re-zipping changed language packs!")
            generate_zipped_srts(
                lang_codes_to_update=lang_codes, download_path=download_path)

            logging.info("Process complete.")
        except Exception as e:
            raise CommandError(e)
