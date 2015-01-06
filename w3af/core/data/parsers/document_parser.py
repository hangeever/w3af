"""
document_parser.py

Copyright 2006 Andres Riancho

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
from stopit import ThreadingTimeout, TimeoutException

from w3af.core.data.parsers.html import HTMLParser
from w3af.core.data.parsers.pdf import PDFParser
from w3af.core.data.parsers.swf import SWFParser
from w3af.core.data.parsers.wml_parser import WMLParser
from w3af.core.data.parsers.javascript import JavaScriptParser
from w3af.core.controllers.exceptions import BaseFrameworkException

import w3af.core.controllers.output_manager as om


class DocumentParser(object):
    """
    This class is a document parser.

    :author: Andres Riancho (andres.riancho@gmail.com)
    """
    # in seconds
    PARSER_TIMEOUT = 60

    # WARNING! The order of this list is important. See note below
    PARSERS = [WMLParser, JavaScriptParser, PDFParser, SWFParser, HTMLParser]

    def __init__(self, http_resp):
        """
        Create the proper parser instance, please note that the order in which
        we ask for the type is not random, first we discard the images which
        account for a great % of the URLs in a site, then we ask for WML which
        is a very specific thing to match, then we try JavaScript, PDF and SWF
        (also very specific) and finally we'll try to parse using the HTMLParser
        which will return True to "can_parse" in lots of cases (even when we're
        unsure that the response is really an HTML document).
        """
        self._parser = None

        if http_resp.is_image():
            msg = 'There is no parser for images.'
            raise BaseFrameworkException(msg)

        for parser in self.PARSERS:
            if parser.can_parse(http_resp):
                try:
                    with ThreadingTimeout(self.PARSER_TIMEOUT, swallow_exc=False):
                        self._parser = parser(http_resp)
                except TimeoutException:
                    msg = '[timeout] The "%s" parser took more than %s seconds'\
                          ' to complete parsing of "%s", killing it!'

                    om.out.debug(msg % (parser.__name__,
                                        self.PARSER_TIMEOUT,
                                        http_resp.get_url()))

        if self._parser is None:
            msg = 'There is no parser for "%s".' % http_resp.get_url()
            raise BaseFrameworkException(msg)

    def get_forms(self):
        """
        :return: A list of forms.
        """
        return self._parser.get_forms()

    def get_references(self):
        """
        :return: A tuple that contains two lists:
            * URL objects extracted through parsing,
            * URL objects extracted through RE matching

        Returned in two separate lists because the first ones
        are much more accurate and they might deserve a different
        treatment.
        """
        return self._parser.get_references()

    def get_references_of_tag(self, tag):
        """
        :param tag: A tag object.
        :return: A list of references related to the tag that is passed as
                 parameter.
        """
        return self._parser.get_references_of_tag(tag)

    def get_emails(self, domain=None):
        """
        :param domain: Indicates what email addresses I want to retrieve:
                       "*@domain".
        :return: A list of email accounts that are inside the document.
        """
        return self._parser.get_emails(domain)

    def get_comments(self):
        """
        :return: A list of comments.
        """
        return self._parser.get_comments()

    def get_meta_redir(self):
        """
        :return: A list of the meta redirection tags.
        """
        return self._parser.get_meta_redir()

    def get_meta_tags(self):
        """
        :return: A list of all meta tags.
        """
        return self._parser.get_meta_tags()

    def get_dom(self):
        """
        :return: The DOM which holds the HTML. Not all parsers return something
                 here. In some cases (like the PDF parser) this returns None.
        """
        return self._parser.get_dom()

    def get_clear_text_body(self):
        """
        :return: Only the text, no tags, which is present in a document.
        """
        return self._parser.get_clear_text_body()


def document_parser_factory(http_resp):
    return DocumentParser(http_resp)
