"""
js.py

Copyright 2019 Andres Riancho

This file is part of w3af, http://w3af.org/ .

w3af is free software; you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation version 2 of the License.

w3af is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with w3af; if not, write to the Free Software
Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA  02110-1301  USA

"""
# pylint: disable=E0401
from darts.lib.utils.lru import SynchronizedLRUDict
# pylint: enable=E0401

import w3af.core.controllers.output_manager as om

from w3af.core.data.misc.xml_bones import get_xml_bones
from w3af.core.controllers.chrome.instrumented.exceptions import EventException, EventTimeout
from w3af.core.controllers.misc.fuzzy_string_cmp import fuzzy_equal
from w3af.core.controllers.chrome.devtools.exceptions import ChromeInterfaceException

NEW_STATE_FOUND = 0
TOO_MANY_PAGE_RELOAD = 1
CONTINUE_WITH_NEXT_EVENTS = 2


class ChromeCrawlerJS(object):
    """
    Extract events from the DOM, dispatch events (click, etc.) and crawl
    the page using chrome
    """

    EVENTS_TO_DISPATCH = {'click',
                          'dblclick'}

    MAX_PAGE_RELOAD = 50
    EQUAL_RATIO_AFTER_BACK = 0.9

    MAX_CONSECUTIVE_EVENT_DISPATCH_ERRORS = 10

    MAX_INITIAL_STATES = 3

    def __init__(self, pool, debugging_id):
        """
        :param pool: Chrome pool
        :param debugging_id: Debugging ID for easier tracking in logs
        """
        self._pool = pool
        self._debugging_id = debugging_id

        self._chrome = None
        self._event_dispatch_log = []
        self._url = None
        self._initial_dom = None
        self._initial_bones_xml = None
        self._reloaded_base_url_count = 0
        self._visited_urls = set()
        self._cached_xml_bones = SynchronizedLRUDict(2)

    def get_name(self):
        return 'JS events'

    def crawl(self,
              chrome,
              url):
        """
        Crawl the page dispatching events in chrome

        :param chrome: The chrome browser where the page is loaded
        :param url: The URL to crawl

        :return: None, all the information is sent to the core via the HTTP
                 traffic queue associated with the chrome instance. This
                 traffic was captured by the browser's proxy and holds all
                 the information for further testing and crawling.
        """
        self._chrome = chrome
        self._url = chrome.get_url()

        try:
            self._crawl_all_states()
        except ChromeInterfaceException as cie:
            msg = ('The JS crawler generated an exception in the chrome'
                   ' interface while crawling %s and will now exit.'
                   ' The exception was: "%s"')
            args = (url, cie)
            om.out.debug(msg % args)

    def _crawl_all_states(self):
        """
        In the JS crawler a state is represented by the browser's DOM. The crawler
        will perform these steps:

             * Load initial URL
             * Retrieve an initial state (DOM)
             * Dispatch events until the initial state changes
             * Start again by loading the initial URL

        Most applications will render the exact same DOM each time a URL is
        requested, other applications which maintain state (that can be changed
        by the event dispatch process) might render different DOMs for the same
        initial URL.

        :return: None, all the information is sent to the core via the HTTP
                 traffic queue associated with the chrome instance. This
                 traffic was captured by the browser's proxy and holds all
                 the information for further testing and crawling.
        """
        successfully_completed = False
        initial_state_counter = 0

        while not successfully_completed and initial_state_counter < self.MAX_INITIAL_STATES:
            try:
                successfully_completed = self._crawl_one_state()
            except MaxPageReload:
                break
            else:
                initial_state_counter += 1

    def _cached_get_xml_bones(self, dom_str):
        try:
            return self._cached_xml_bones[dom_str]
        except KeyError:
            xml_bones = get_xml_bones(dom_str)
            self._cached_xml_bones[dom_str] = xml_bones
            return xml_bones

    def _crawl_one_state(self):
        """
        Dispatch events in one state (DOM) until the state changes enough that
        it makes no sense to keep dispatching events.

        :return: None, all the information is sent to the core via the HTTP
                 traffic queue associated with the chrome instance. This
                 traffic was captured by the browser's proxy and holds all
                 the information for further testing and crawling.
        """
        self._initial_dom = self._chrome.get_dom()
        self._initial_bones_xml = self._cached_get_xml_bones(self._initial_dom)

        event_listeners = self._chrome.get_all_event_listeners(event_filter=self.EVENTS_TO_DISPATCH)

        for event_i, event in enumerate(event_listeners):

            if not self._should_dispatch_event(event):
                continue

            # Dispatch the event
            self._dispatch_event(event)

            # Logging
            self._print_stats(event_i)

            # Handle any side-effects (such as browsing to a different page or
            # big DOM changes that will break the next event dispatch calls)
            result = self._handle_event_dispatch_side_effects()

            if result == CONTINUE_WITH_NEXT_EVENTS:
                continue

            elif result == NEW_STATE_FOUND:
                # It makes no sense to keep sending events to this state because
                # the current DOM is very different from the one we initially
                # inspected to retrieve the event listeners from
                return False

            elif result == TOO_MANY_PAGE_RELOAD:
                # Too many full page reloads, need to exit
                raise MaxPageReload()

        #
        # We were able to send all events to initial state and no more states
        # were identified nor need testing
        #
        # Give the browser a second to finish up processing of all the events
        # we just fired, the last event might have triggered some action that
        # is not completed yet and we don't want to miss
        #
        self._chrome.wait_for_load(0.5)
        self._chrome.navigation_started(0.5)

        return True

    def _conditional_wait_for_load(self):
        potentially_new_url = self._chrome.get_url()

        if potentially_new_url in self._visited_urls:
            return

        self._visited_urls.add(potentially_new_url)
        self._chrome.wait_for_load(timeout=1)

    def _handle_event_dispatch_side_effects(self):
        """
        The algorithm was designed to dispatch a lot of events without performing
        a strict check on how that affects the DOM, or if the browser navigates
        to a different URL.

        Algorithms that performed strict checks after dispatching events all
        ended up in slow beasts.

        The key word here is *strict*. This algorithm DOES perform checks to
        verify if the DOM has changed / the page navigated to a different URL,
        but these checks are performed in a non-blocking way:

            Never wait for any specific lifeCycle event that the browser might
            or might not send!

        :return: One of the following:
                    - NEW_STATE_FOUND
                    - TOO_MANY_PAGE_RELOAD
                    - CONTINUE_WITH_NEXT_EVENTS
        """
        try:
            current_dom = self._chrome.get_dom()
        except ChromeInterfaceException:
            #
            # We get here when the DOM is not loaded yet. This is most likely
            # because the event triggered a page navigation, wait a few seconds
            # for the page to load (this will give the w3af core more info to
            # process and more crawling points) and then go back to the initial
            # URL
            #
            self._conditional_wait_for_load()
            self._reload_base_url()
            current_dom = self._chrome.get_dom()
        else:
            #
            # We get here in two cases
            #
            #   a) The browser navigated to a different URL *really quickly*
            #      and was able to create a DOM for us in that new URL
            #
            #   b) The browser is still in the initial URL and the DOM we're
            #      seeing is the one associated with that URL
            #
            # If we're in a), we want to reload the initial URL
            #
            potentially_new_url = self._chrome.get_url()

            if potentially_new_url != self._url:
                #
                # Let this new page load for 1 second so that new information
                # reaches w3af's core, and after that render the initial URL
                # again
                #
                self._conditional_wait_for_load()
                self._reload_base_url()
                current_dom = self._chrome.get_dom()

        current_bones_xml = self._cached_get_xml_bones(current_dom)

        #
        # The DOM did change! Something bad happen!
        #
        if not self._bones_xml_are_equal(self._initial_bones_xml, current_bones_xml):
            msg = ('The JS crawler reloaded the initial URL and noticed'
                   ' a big change in the DOM. This usually happens when'
                   ' the application changes state. This happen while'
                   ' crawling %s, the process will stop (did: %s)')
            args = (self._url, self._debugging_id)
            om.out.debug(msg % args)
            return NEW_STATE_FOUND

        #
        # Checks that might break the for loop
        #
        if self._reloaded_base_url_count > self.MAX_PAGE_RELOAD:
            msg = ('The JS crawler had to perform more than %s page reloads'
                   ' while crawling %s, the process will stop (did: %s)')
            args = (self._url, self.MAX_PAGE_RELOAD, self._debugging_id)
            om.out.debug(msg % args)
            return TOO_MANY_PAGE_RELOAD

        last_dispatch_results = self._event_dispatch_log[:self.MAX_CONSECUTIVE_EVENT_DISPATCH_ERRORS]
        last_dispatch_results = [el.state for el in last_dispatch_results]

        all_failed = True
        for state in last_dispatch_results:
            if state != EventDispatchLogUnit.FAILED:
                all_failed = False
                break

        if all_failed:
            msg = ('Too many consecutive event dispatch errors were found while'
                   ' crawling %s, the process will stop (did: %s)')
            args = (self._url, self._debugging_id)
            om.out.debug(msg % args)
            return NEW_STATE_FOUND

        return CONTINUE_WITH_NEXT_EVENTS

    def _bones_xml_are_equal(self, bones_xml_a, bones_xml_b):
        return fuzzy_equal(bones_xml_a,
                           bones_xml_b,
                           self.EQUAL_RATIO_AFTER_BACK)

    def _print_stats(self, event_i):
        event_types = {}

        for event_dispatch_log_unit in self._event_dispatch_log:
            event_type = event_dispatch_log_unit.event['event_type']
            if event_type in event_types:
                event_types[event_type] += 1
            else:
                event_types[event_type] = 1

        msg = ('Processing event %s out of (unknown) for %s.'
               ' Event dispatch error count is %s.'
               ' Already processed %s events with types: %r. (did: %s)')
        args = (event_i,
                self._url,
                self._get_total_dispatch_error_count(),
                self._get_total_dispatch_count(),
                event_types,
                self._debugging_id)

        om.out.debug(msg % args)

    def _get_total_dispatch_error_count(self):
        return len([i for i in self._event_dispatch_log if i.state == EventDispatchLogUnit.FAILED])

    def _get_total_dispatch_count(self):
        return len([i for i in self._event_dispatch_log if i.state != EventDispatchLogUnit.IGNORED])

    def _dispatch_event(self, event):
        selector = event['selector']
        event_type = event['event_type']

        msg = 'Dispatching "%s" on CSS selector "%s" at page %s (did: %s)'
        args = (event_type, selector, self._url, self._debugging_id)
        om.out.debug(msg % args)

        try:
            self._chrome.dispatch_js_event(selector, event_type)
        except EventException:
            msg = ('The "%s" event on CSS selector "%s" at page %s failed'
                   ' to run because the element does not exist anymore'
                   ' (did: %s)')
            args = (event_type, selector, self._url, self._debugging_id)
            om.out.debug(msg % args)

            event_dispatch_log_unit = EventDispatchLogUnit(event, EventDispatchLogUnit.FAILED)
            self._event_dispatch_log.append(event_dispatch_log_unit)

            return False

        except EventTimeout:
            msg = ('The "%s" event on CSS selector "%s" at page %s failed'
                   ' to run in the given time (did: %s)')
            args = (event_type, selector, self._url, self._debugging_id)
            om.out.debug(msg % args)

            event_dispatch_log_unit = EventDispatchLogUnit(event, EventDispatchLogUnit.FAILED)
            self._event_dispatch_log.append(event_dispatch_log_unit)

            return False

        event_dispatch_log_unit = EventDispatchLogUnit(event, EventDispatchLogUnit.SUCCESS)
        self._event_dispatch_log.append(event_dispatch_log_unit)

        return True

    def _reload_base_url(self):
        self._reloaded_base_url_count += 1
        self._chrome.load_url(self._url)
        return self._chrome.wait_for_load()

    def _ignore_event(self, event):
        event_dispatch_log_unit = EventDispatchLogUnit(event, EventDispatchLogUnit.IGNORED)
        self._event_dispatch_log.append(event_dispatch_log_unit)

    def _should_dispatch_event(self, event):
        """
        :param event: The event to analyze
        :return: True if this event should be dispatched to the browser
        """
        current_event_type = event['event_type']
        current_event_key = event.get_type_selector()

        # Only dispatch events if type in EVENTS_TO_DISPATCH
        if current_event_type not in self.EVENTS_TO_DISPATCH:
            self._ignore_event(event)
            return False

        # Do not dispatch the same event twice
        for event_dispatch_log_unit in self._event_dispatch_log:
            if current_event_key != event_dispatch_log_unit.event.get_type_selector():
                continue

            # Unless the first time we tries to dispatch it we failed
            if event_dispatch_log_unit.state == EventDispatchLogUnit.FAILED:
                return True

            msg = ('Ignoring "%s" event on selector "%s" and URL "%s"'
                   ' because it was already sent. This happens when the'
                   ' application attaches more than one event listener'
                   ' to the same event and element. (did: %s)')
            args = (current_event_type,
                    event['selector'],
                    self._url,
                    self._debugging_id)
            om.out.debug(msg % args)

            self._ignore_event(event)
            return False

        return True


class EventDispatchLogUnit(object):
    IGNORED = 0
    SUCCESS = 1
    FAILED = 2

    def __init__(self, event, state):
        assert state in (self.IGNORED, self.SUCCESS, self.FAILED), 'Invalid state'

        self.state = state
        self.event = event


class MaxPageReload(Exception):
    pass
