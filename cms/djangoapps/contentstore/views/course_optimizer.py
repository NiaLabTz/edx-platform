"""
These views handle all actions in Studio related to link checking of
courses
"""


import base64
import json
import logging
import os
import re
import requests
import shutil
from wsgiref.util import FileWrapper

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.files import File
from django.core.files.storage import FileSystemStorage
from django.db import transaction
from django.http import Http404, HttpResponse, HttpResponseNotFound, StreamingHttpResponse
from django.shortcuts import redirect
from django.utils.translation import gettext as _
from django.views.decorators.cache import cache_control
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_http_methods
from edx_django_utils.monitoring import set_custom_attribute, set_custom_attributes_for_course_key
from opaque_keys.edx.keys import CourseKey
from path import Path as path
from storages.backends.s3boto3 import S3Boto3Storage
from user_tasks.conf import settings as user_tasks_settings
from user_tasks.models import UserTaskArtifact, UserTaskStatus

from common.djangoapps.edxmako.shortcuts import render_to_response
from common.djangoapps.static_replace import replace_static_urls
from common.djangoapps.student.auth import has_course_author_access
from common.djangoapps.util.json_request import JsonResponse
from common.djangoapps.util.monitoring import monitor_import_failure
from common.djangoapps.util.views import ensure_valid_course_key
from cms.djangoapps.contentstore.xblock_storage_handlers.view_handlers import get_xblock
from cms.djangoapps.contentstore.xblock_storage_handlers.xblock_helpers import usage_key_with_run
from xmodule.modulestore.django import modulestore  # lint-amnesty, pylint: disable=wrong-import-order

from ..storage import course_import_export_storage
from ..tasks import CourseLinkCheckTask, check_broken_links
from ..utils import reverse_course_url, reverse_usage_url

__all__ = [
    'link_check_handler',
    'link_check_status_handler',
]

log = logging.getLogger(__name__)

STATUS_FILTERS = user_tasks_settings.USER_TASKS_STATUS_FILTERS


def send_tarball(tarball, size):
    """
    Renders a tarball to response, for use when sending a tar.gz file to the user.
    """
    wrapper = FileWrapper(tarball, settings.COURSE_EXPORT_DOWNLOAD_CHUNK_SIZE)
    response = StreamingHttpResponse(wrapper, content_type='application/x-tgz')
    response['Content-Disposition'] = 'attachment; filename=%s' % os.path.basename(tarball.name)
    response['Content-Length'] = size
    return response


@transaction.non_atomic_requests
@ensure_csrf_cookie
@login_required
@require_http_methods(('GET', 'POST'))
@ensure_valid_course_key
def link_check_handler(request, course_key_string):
    """
    The restful handler for checking broken links in a course.

    GET
        html: return html page for import page ???
        json: not supported ???
    POST
        Start a Celery task to check broken links in the course

    The Studio UI uses a POST request to start the export asynchronously, with
    a link appearing on the page once it's ready.
    """
    course_key = CourseKey.from_string(course_key_string)
    if not has_course_author_access(request.user, course_key):
        raise PermissionDenied()
    courselike_block = modulestore().get_course(course_key)
    if courselike_block is None:
        raise Http404
    context = {
        'context_course': courselike_block,
        'courselike_home_url': reverse_course_url("course_handler", course_key),
    }
    context['status_url'] = reverse_course_url('export_status_handler', course_key)

    # an _accept URL parameter will be preferred over HTTP_ACCEPT in the header.
    requested_format = request.GET.get('_accept', request.META.get('HTTP_ACCEPT', 'text/html'))

    if request.method == 'POST':
        check_broken_links.delay(request.user.id, course_key_string, request.LANGUAGE_CODE)
        return JsonResponse({'LinkCheckStatus': 1})
    else:
        # Only HTML request format is supported (no JSON).
        return HttpResponse(status=406)


@transaction.non_atomic_requests
@require_GET
@ensure_csrf_cookie
@login_required
@ensure_valid_course_key
def link_check_status_handler(request, course_key_string):
    """
    Returns an integer corresponding to the status of a link check. These are:

        -X : Link check unsuccessful due to some error with X as stage [0-3]
        0 : No status info found (task not yet created)
        1 : Scanning
        2 : Verifying
        3 : Success

    If the link check was successful, an output result is also returned.
    """
    course_key = CourseKey.from_string(course_key_string)
    if not has_course_author_access(request.user, course_key):
        raise PermissionDenied()

    # The task status record is authoritative once it's been created
    task_status = _latest_task_status(request, course_key_string, link_check_status_handler)
    json_content = None
    test = None
    response = None
    error = None
    broken_links_dto = None
    if task_status is None:
        # The task hasn't been initialized yet; did we store info in the session already?
        try:
            session_status = request.session["link_check_status"]
            status = session_status[course_key_string]
        except KeyError:
            status = 0
    elif task_status.state == UserTaskStatus.SUCCEEDED:
        status = 3
        artifact = UserTaskArtifact.objects.get(status=task_status, name='BrokenLinks')
        with artifact.file as file:
            content = file.read()
            json_content = json.loads(content)
            broken_links_dto = _create_dto(json_content, request.user)
    elif task_status.state in (UserTaskStatus.FAILED, UserTaskStatus.CANCELED):
        status = max(-(task_status.completed_steps + 1), -2)
        errors = UserTaskArtifact.objects.filter(status=task_status, name='Error')
        if errors:
            error = errors[0].text
            try:
                error = json.loads(error)
            except ValueError:
                # Wasn't JSON, just use the value as a string
                pass
    else:
        status = min(task_status.completed_steps + 1, 2)

    response = {
        "LinkCheckStatus": status,
    }
    if broken_links_dto:
        response["LinkCheckOutput"] = broken_links_dto
    # if json_content:
    #     response['debug'] = json_content
    if error:
        response['LinkCheckError'] = error
    return JsonResponse(response)


def _latest_task_status(request, course_key_string, view_func=None):
    """
    Get the most recent link check status update for the specified course
    key.
    """
    args = {'course_key_string': course_key_string}
    name = CourseLinkCheckTask.generate_name(args)
    task_status = UserTaskStatus.objects.filter(name=name)
    for status_filter in STATUS_FILTERS:
        task_status = status_filter().filter_queryset(request, task_status, view_func)
    return task_status.order_by('-created').first()


def _create_dto(json_content, request_user):
    """
    Returns a DTO for frontend given a list of broken links.

    json_content contains a list of the following:
        [block_id, link]

    Returned DTO structure:
    {
        section: {
            display_name,
            subsection: {
                display_name,
                unit: {
                    display_name,
                    block: {
                        display_name,
                        url,
                        broken_links: [],
                    }
                }
            }
        }
    }
    """
    result = {}
    for item in json_content:
        block_id, link = item
        usage_key = usage_key_with_run(block_id)
        block = get_xblock(usage_key, request_user)
        _add_broken_link(result, block, link)

    return result


def _add_broken_link(result, block, link):
    """
    Adds broken link found in the specified block along with other block data.
    Note that because the celery queue does not have credentials, some broken links will
    need to be checked client side.
    """
    hierarchy = []
    current = block
    while current:
        hierarchy.append(current)
        current = current.get_parent()
    
    current_dict = result
    for xblock in reversed(hierarchy):
        current_dict = current_dict.setdefault(
            str(xblock.location.block_id), 
            { 'display_name': xblock.display_name }
        )
    
    current_dict['url'] = f'/course/{block.course_id}/editor/{block.category}/{block.location}'
    current_dict.setdefault('broken_links', []).append(link)
