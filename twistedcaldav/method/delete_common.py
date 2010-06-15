##
# Copyright (c) 2006-2010 Apple Inc. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
##


"""
CalDAV DELETE behaviors.
"""

__all__ = ["DeleteResource"]

from twisted.internet.defer import inlineCallbacks, returnValue
from twext.web2 import responsecode
from twext.web2.dav.fileop import delete
from twext.web2.dav.http import ResponseQueue, MultiStatusResponse
from twext.web2.dav.util import joinURL
from twext.web2.http import HTTPError, StatusResponse

from twext.python.log import Logger
from twext.web2.dav.http import ErrorResponse

from twistedcaldav.caldavxml import caldav_namespace, ScheduleTag
from twistedcaldav.config import config
from twistedcaldav.customxml import calendarserver_namespace
from twistedcaldav.memcachelock import MemcacheLock, MemcacheLockTimeoutError
from twistedcaldav.method.report_common import applyToAddressBookCollections, applyToCalendarCollections
from twistedcaldav.resource import isCalendarCollectionResource,\
    isPseudoCalendarCollectionResource, isAddressBookCollectionResource
from twistedcaldav.scheduling.implicit import ImplicitScheduler

log = Logger()

class DeleteResource(object):
    
    def __init__(self, request, resource, resource_uri, parent, depth,
        internal_request=False, allowImplicitSchedule=True):
        
        self.request = request
        self.resource = resource
        self.resource_uri = resource_uri
        self.parent = parent
        self.depth = depth
        self.internal_request = internal_request
        self.allowImplicitSchedule = allowImplicitSchedule

    def validIfScheduleMatch(self):
        """
        Check for If-ScheduleTag-Match header behavior.
        """
        
        # Only when a direct request
        if not self.internal_request:
            header = self.request.headers.getHeader("If-Schedule-Tag-Match")
            if header:
                # Do "precondition" test
                matched = False
                if self.resource.exists() and self.resource.hasDeadProperty(ScheduleTag):
                    scheduletag = self.resource.readDeadProperty(ScheduleTag)
                    matched = (scheduletag == header)
                if not matched:
                    log.debug("If-Schedule-Tag-Match: header value '%s' does not match resource value '%s'" % (header, scheduletag,))
                    raise HTTPError(responsecode.PRECONDITION_FAILED)
            
            elif config.Scheduling.CalDAV.ScheduleTagCompatibility:
                # Actually by the time we get here the pre-condition will already have been tested and found to be OK
                # (CalDAVFile.checkPreconditions) so we can ignore this case.
                pass

    @inlineCallbacks
    def deleteResource(self, delresource, deluri, parent):
        """
        Delete a plain resource which may be a collection - but only one not containing
        calendar resources.

        @param delresource:
        @type delresource:
        @param deluri:
        @type deluri:
        @param parent:
        @type parent:
        """

        # Do quota checks before we start deleting things
        myquota = (yield delresource.quota(self.request))
        if myquota is not None:
            old_size = (yield delresource.quotaSize(self.request))
        else:
            old_size = 0
        
        # Do delete
        response = (yield delete(deluri, delresource.fp, self.depth))

        # Adjust quota
        if myquota is not None:
            yield delresource.quotaSizeAdjust(self.request, -old_size)

        if response == responsecode.NO_CONTENT:
            if isPseudoCalendarCollectionResource(parent):
                newrevision = (yield parent.bumpSyncToken())
                index = parent.index()
                index.deleteResource(delresource.fp.basename(), newrevision)
                
        returnValue(response)

    @inlineCallbacks
    def deleteCalendarResource(self, delresource, deluri, parent):
        """
        Delete a single calendar resource and do implicit scheduling actions if required.

        @param delresource:
        @type delresource:
        @param deluri:
        @type deluri:
        @param parent:
        @type parent:
        """

        # TODO: need to use transaction based delete on live scheduling object resources
        # as the iTIP operation may fail and may need to prevent the delete from happening.
    
        # Do If-Schedule-Tag-Match behavior first
        self.validIfScheduleMatch()

        # Do quota checks before we start deleting things
        myquota = (yield delresource.quota(self.request))
        if myquota is not None:
            old_size = (yield delresource.quotaSize(self.request))
        else:
            old_size = 0
        
        scheduler = None
        lock = None
        if not self.internal_request and self.allowImplicitSchedule:
            # Get data we need for implicit scheduling
            calendar = (yield delresource.iCalendarForUser(self.request))
            scheduler = ImplicitScheduler()
            do_implicit_action, _ignore = (yield scheduler.testImplicitSchedulingDELETE(self.request, delresource, calendar))
            if do_implicit_action:
                # Cannot do implicit in sharee's shared calendar
                isvirt = (yield parent.isVirtualShare(self.request))
                if isvirt:
                    raise HTTPError(ErrorResponse(
                        responsecode.FORBIDDEN,
                        (calendarserver_namespace, "sharee-privilege-needed",),
                        description="Sharee's cannot schedule"
                    ))
                lock = MemcacheLock("ImplicitUIDLock", calendar.resourceUID(), timeout=60.0)

        try:
            if lock:
                yield lock.acquire()
    
            # Do delete
            response = (yield delete(deluri, delresource.fp, self.depth))

            # Adjust quota
            if myquota is not None:
                yield delresource.quotaSizeAdjust(self.request, -old_size)
    
            if response == responsecode.NO_CONTENT:
                newrevision = (yield parent.bumpSyncToken())
                index = parent.index()
                index.deleteResource(delresource.fp.basename(), newrevision)
    
                # Do scheduling
                if scheduler and not self.internal_request and self.allowImplicitSchedule:
                    yield scheduler.doImplicitScheduling()
    
        except MemcacheLockTimeoutError:
            raise HTTPError(StatusResponse(responsecode.CONFLICT, "Resource: %s currently in use on the server." % (deluri,)))
    
        finally:
            if lock:
                yield lock.clean()
                
        returnValue(response)

    @inlineCallbacks
    def deleteCalendar(self, delresource, deluri, parent):
        """
        Delete an entire calendar collection by deleting each child resource in turn to
        ensure that proper implicit scheduling actions occur.
        
        This has to emulate the behavior in fileop.delete in that any errors need to be
        reported back in a multistatus response.
        """

        # Not allowed to delete the default calendar
        default = (yield delresource.isDefaultCalendar(self.request))
        if default:
            log.err("Cannot DELETE default calendar: %s" % (delresource,))
            raise HTTPError(ErrorResponse(responsecode.FORBIDDEN, (caldav_namespace, "default-calendar-delete-allowed",)))

        if self.depth != "infinity":
            msg = "Client sent illegal depth header value for DELETE: %s" % (self.depth,)
            log.err(msg)
            raise HTTPError(StatusResponse(responsecode.BAD_REQUEST, msg))

        # Check virtual share first
        isVirtual = yield delresource.isVirtualShare(self.request)
        if isVirtual:
            log.debug("Removing shared calendar %s" % (delresource,))
            yield delresource.removeVirtualShare(self.request)
            returnValue(responsecode.NO_CONTENT)

        log.debug("Deleting calendar %s" % (delresource.fp.path,))

        errors = ResponseQueue(deluri, "DELETE", responsecode.NO_CONTENT)

        for childname in delresource.listChildren():

            childurl = joinURL(deluri, childname)
            child = (yield self.request.locateChildResource(delresource, childname))

            try:
                yield self.deleteCalendarResource(child, childurl, delresource)
            except:
                errors.add(childurl, responsecode.BAD_REQUEST)

        # Now do normal delete

        # Handle sharing
        wasShared = (yield delresource.isShared(self.request))
        if wasShared:
            yield delresource.downgradeFromShare(self.request)

        # Change CTag
        yield delresource.bumpSyncToken()
        more_responses = (yield self.deleteResource(delresource, deluri, parent))
        
        if isinstance(more_responses, MultiStatusResponse):
            # Merge errors
            errors.responses.update(more_responses.children)                

        response = errors.response()
        
        if response == responsecode.NO_CONTENT:
            # Do some clean up
            yield delresource.deletedCalendar(self.request)

        returnValue(response)

    @inlineCallbacks
    def deleteCollection(self):
        """
        Delete a regular collection with special processing for any calendar collections
        contained within it.
        """
        if self.depth != "infinity":
            msg = "Client sent illegal depth header value for DELETE: %s" % (self.depth,)
            log.err(msg)
            raise HTTPError(StatusResponse(responsecode.BAD_REQUEST, msg))

        log.debug("Deleting collection %s" % (self.resource.fp.path,))

        errors = ResponseQueue(self.resource_uri, "DELETE", responsecode.NO_CONTENT)
 
        @inlineCallbacks
        def doDeleteCalendar(delresource, deluri):
            
            delparent = (yield delresource.locateParent(self.request, deluri))

            response = (yield self.deleteCalendar(delresource, deluri, delparent))

            if isinstance(response, MultiStatusResponse):
                # Merge errors
                errors.responses.update(response.children)                

            returnValue(True)

        yield applyToCalendarCollections(self.resource, self.request, self.resource_uri, self.depth, doDeleteCalendar, None)

        # Now do normal delete
        more_responses = (yield self.deleteResource(self.resource, self.resource_uri, self.parent))
        
        if isinstance(more_responses, MultiStatusResponse):
            # Merge errors
            errors.responses.update(more_responses.children)                

        response = errors.response()

        returnValue(response)

    @inlineCallbacks
    def deleteAddressBookResource(self, delresource, deluri, parent):
        """
        Delete a single addressbook resource and do implicit scheduling actions if required.

        @param delresource:
        @type delresource:
        @param deluri:
        @type deluri:
        @param parent:
        @type parent:
        """

        # TODO: need to use transaction based delete on live scheduling object resources
        # as the iTIP operation may fail and may need to prevent the delete from happening.
    
        # Do quota checks before we start deleting things
        myquota = (yield delresource.quota(self.request))
        if myquota is not None:
            old_size = (yield delresource.quotaSize(self.request))
        else:
            old_size = 0
        
        try:
    
            # Do delete
            response = (yield delete(deluri, delresource.fp, self.depth))

            # Adjust quota
            if myquota is not None:
                yield delresource.quotaSizeAdjust(self.request, -old_size)
    
            if response == responsecode.NO_CONTENT:
                newrevision = (yield parent.bumpSyncToken())
                index = parent.index()
                index.deleteResource(delresource.fp.basename(), newrevision)    
    
        except MemcacheLockTimeoutError:
            raise HTTPError(StatusResponse(responsecode.CONFLICT, "Resource: %s currently in use on the server." % (deluri,)))
    
                
        returnValue(response)

    @inlineCallbacks
    def deleteAddressBook(self, delresource, deluri, parent):
        """
        Delete an entire addressbook collection by deleting each child resource in turn to
        ensure that proper implicit scheduling actions occur.
        
        This has to emulate the behavior in fileop.delete in that any errors need to be
        reported back in a multistatus response.
        """


        if self.depth != "infinity":
            msg = "Client sent illegal depth header value for DELETE: %s" % (self.depth,)
            log.err(msg)
            raise HTTPError(StatusResponse(responsecode.BAD_REQUEST, msg))

        # Check virtual share first
        isVirtual = yield delresource.isVirtualShare(self.request)
        if isVirtual:
            log.debug("Removing shared address book %s" % (delresource,))
            yield delresource.removeVirtualShare(self.request)
            returnValue(responsecode.NO_CONTENT)

        log.debug("Deleting addressbook %s" % (delresource.fp.path,))

        errors = ResponseQueue(deluri, "DELETE", responsecode.NO_CONTENT)

        for childname in delresource.listChildren():

            childurl = joinURL(deluri, childname)
            child = (yield self.request.locateChildResource(delresource, childname))

            try:
                yield self.deleteAddressBookResource(child, childurl, delresource)
            except:
                errors.add(childurl, responsecode.BAD_REQUEST)

        # Now do normal delete

        # Handle sharing
        wasShared = (yield delresource.isShared(self.request))
        if wasShared:
            yield delresource.downgradeFromShare(self.request)

        yield delresource.bumpSyncToken()
        more_responses = (yield self.deleteResource(delresource, deluri, parent))
        
        if isinstance(more_responses, MultiStatusResponse):
            # Merge errors
            errors.responses.update(more_responses.children)                

        response = errors.response()
        
        returnValue(response)

    @inlineCallbacks
    def deleteCollectionAB(self):
        # XXX CSCS-MERGE this needs to be merged into deleteCollection
        """
        Delete a regular collection with special processing for any addressbook collections
        contained within it.
        """
        if self.depth != "infinity":
            msg = "Client sent illegal depth header value for DELETE: %s" % (self.depth,)
            log.err(msg)
            raise HTTPError(StatusResponse(responsecode.BAD_REQUEST, msg))

        log.debug("Deleting collection %s" % (self.resource.fp.path,))

        errors = ResponseQueue(self.resource_uri, "DELETE", responsecode.NO_CONTENT)
 
        @inlineCallbacks
        def doDeleteAddressBook(delresource, deluri):
            
            delparent = (yield delresource.locateParent(self.request, deluri))

            response = (yield self.deleteAddressBook(delresource, deluri, delparent))

            if isinstance(response, MultiStatusResponse):
                # Merge errors
                errors.responses.update(response.children)                

            returnValue(True)

        yield applyToAddressBookCollections(self.resource, self.request, self.resource_uri, self.depth, doDeleteAddressBook, None)

        # Now do normal delete
        more_responses = (yield self.deleteResource(self.resource, self.resource_uri, self.parent))
        
        if isinstance(more_responses, MultiStatusResponse):
            # Merge errors
            errors.responses.update(more_responses.children)                

        response = errors.response()

        returnValue(response)

    @inlineCallbacks
    def run(self):

        if isCalendarCollectionResource(self.parent):
            response = (yield self.deleteCalendarResource(self.resource, self.resource_uri, self.parent))
            
        elif isCalendarCollectionResource(self.resource):
            response = (yield self.deleteCalendar(self.resource, self.resource_uri, self.parent))

        elif isAddressBookCollectionResource(self.parent):
            response = (yield self.deleteAddressBookResource(self.resource, self.resource_uri, self.parent))

        elif isAddressBookCollectionResource(self.resource):
            response = (yield self.deleteAddressBook(self.resource, self.resource_uri, self.parent))

        elif self.resource.isCollection():
            response = (yield self.deleteCollection())

        else:
            response = (yield self.deleteResource(self.resource, self.resource_uri, self.parent))

        returnValue(response)
