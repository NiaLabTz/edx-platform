from openedx_events.learning.signals import USER_NOTIFICATION_REQUESTED
from openedx_events.learning.data import UserNotificationData
from openedx.core.djangoapps.django_comment_common.comment_client.comment import Comment
from django.conf import settings


def send_response_notifications(thread, course, creator, parent_id=None):
    """
    Send notifications to users who are subscribed to the thread.
    """
    notification_sender = DiscussionNotificationSender(thread, course, creator, parent_id)
    notification_sender.send_new_comment_notification()
    notification_sender.send_new_response_notification()
    notification_sender.send_new_comment_on_response_notification()
    notification_sender.send_response_on_followed_post_notification()


class DiscussionNotificationSender:
    """
    Class to send notifications to users who are subscribed to the thread.
    """

    def __init__(self, thread, course, creator, parent_id=None):
        self.thread = thread
        self.course = course
        self.creator = creator
        self.parent_id = parent_id
        self.parent_response = None
        self._get_parent_response()

    def _send_notification(self, user_ids, notification_type, extra_context=None):
        """
        Send notification to users
        """
        if not user_ids:
            return

        if extra_context is None:
            extra_context = {}

        notification_data = UserNotificationData(
            user_ids=user_ids,
            context={
                "replier_name": self.creator.username,
                "post_title": self.thread.title,
                "course_name": self.course.display_name,
                **extra_context,
            },
            notification_type=notification_type,
            content_url=f"{settings.DISCUSSIONS_MICROFRONTEND_URL}/{str(self.course.id)}/posts/{self.thread.id}",
            app_name="discussion",
            course_key=self.course.id,
        )
        USER_NOTIFICATION_REQUESTED.send_event(notification_data=notification_data)

    def _get_parent_response(self):
        """
        Get parent response object
        """
        if self.parent_id and not self.parent_response:
            self.parent_response = Comment(id=self.parent_id).retrieve()

        return self.parent_response

    def send_new_response_notification(self):
        """
        Send notification to users who are subscribed to the main thread/post i.e.
        there is a response to the main thread.
        """
        if not self.parent_id and self.creator.id != int(self.thread.user_id):
            self._send_notification([self.thread.user_id], "new_response")

    def send_new_comment_notification(self):
        """
        Send notification to parent thread creator i.e. comment on the response.
        """
        if self.parent_response and self.creator.id != int(self.thread.user_id):
            context = {
                "author_name": self.parent_response.username,
            }
            self._send_notification([self.thread.user_id], "new_comment", extra_context=context)

    def send_new_comment_on_response_notification(self):
        """
        Send notification to parent response creator i.e. comment on the response.
        """
        if self.parent_response and self.creator.id != int(self.parent_response.user_id):
            self._send_notification([self.parent_response.user_id], "new_comment_on_response")

    def send_response_on_followed_post_notification(self):
        """
        Send notification to followers of the thread/post
        except:
        Tread creator , response creator,
        """
        if self.parent_response:
            users = []
            for subscriber in self.thread.subscribers:
                subscriber_user_id = int(subscriber['_id'])

                if subscriber_user_id not in [self.thread.user_id , self.parent_response['user_id']]:
                    users.append(subscriber_user_id)
            breakpoint()
            self._send_notification([self.parent_response.user_id], "response_on_followed_post")
