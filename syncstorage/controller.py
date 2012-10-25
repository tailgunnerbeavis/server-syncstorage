# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.
"""
Storage controller. Implements all info, user APIs from:

http://docs.services.mozilla.com/storage/apis-2.0.html

"""
import simplejson as json
import functools

from pyramid.httpexceptions import (HTTPBadRequest,
                                    HTTPNotFound,
                                    HTTPConflict,
                                    HTTPPreconditionFailed,
                                    HTTPCreated,
                                    HTTPNoContent,
                                    HTTPNotModified,
                                    HTTPUnsupportedMediaType)

from mozsvc.exceptions import (ERROR_MALFORMED_JSON, ERROR_INVALID_OBJECT,
                               ERROR_OVER_QUOTA)

from syncstorage.bso import BSO
from syncstorage.storage import (get_storage, ConflictError,
                                 NotFoundError, InvalidOffsetError)

_ONE_MEG = 1024 * 1024

# The maximum number of ids that can be deleted in a single batch operation.
MAX_IDS_PER_BATCH = 100

# How long the client should wait before retrying a conflicting write.
RETRY_AFTER = 5


def HTTPJsonBadRequest(data, **kwds):
    kwds.setdefault("content_type", "application/json")
    return HTTPBadRequest(body=json.dumps(data, use_decimal=True), **kwds)


def with_read_lock(func):
    """Method decorator to take a collection-level read lock.

    Methods decorated with this decorator will take a read lock on their
    target collection for the duration of the method.
    """
    @functools.wraps(func)
    def with_read_lock_wrapper(self, request, *args, **kwds):
        storage = self._get_storage(request)
        userid = request.user["uid"]
        collection = request.matchdict.get("collection")
        with storage.lock_for_read(userid, collection):
            return func(self, request, *args, **kwds)
    return with_read_lock_wrapper


def with_write_lock(func):
    """Method decorator to take a collection-level write lock.

    Methods decorated with this decorator will take a write lock on their
    target collection for the duration of the method.
    """
    @functools.wraps(func)
    def with_write_lock_wrapper(self, request, *args, **kwds):
        storage = self._get_storage(request)
        userid = request.user["uid"]
        collection = request.matchdict.get("collection")
        with storage.lock_for_write(userid, collection):
            return func(self, request, *args, **kwds)
    return with_write_lock_wrapper


def convert_storage_errors(func):
    """Function decorator to turn storage backend errors into HTTP errors.

    This decorator does a simple mapping from the following storage backend
    errors to their corresponding HTTP errors:

        NotFoundError => HTTPNotFound
        ConflictError => HTTPConflict

    """
    @functools.wraps(func)
    def error_converter_wrapper(*args, **kwds):
        try:
            return func(*args, **kwds)
        except NotFoundError:
            raise HTTPNotFound
        except ConflictError:
            raise HTTPConflict(headers={"Retry-After": str(RETRY_AFTER)})
        except InvalidOffsetError:
            raise HTTPBadRequest("Invalid offset token")
    return error_converter_wrapper


class StorageController(object):

    def __init__(self, config):
        settings = config.registry.settings
        self.logger = config.registry['metlog']
        self.quota_size = settings.get("storage.quota_size", None)
        self.batch_max_count = settings.get('storage.batch_max_count',
                                            100)
        self.batch_max_bytes = settings.get('storage.batch_max_bytes',
                                            _ONE_MEG)

    def _get_storage(self, request):
        return get_storage(request)

    def _check_precondition_headers(self, request):
        """Check the X-If-[Un|M]odified-Since-Version headers.

        This method checks the version of the target resource against the
        X-If-Modified-Since-Version and X-If-Unmodified-Since-Version headers,
        returning an appropriate "304 Not Modified" or "412 Precondition l
        Failed" response if required.

        It also has the side-effect of setting X-Last-Modified-Version in the
        response headers.
        """
        modified = request.headers.get("X-If-Modified-Since-Version")
        unmodified = request.headers.get("X-If-Unmodified-Since-Version")

        if modified is not None and unmodified is not None:
            msg = "X-If-Modified-Since-Version and "
            msg += "X-If-Unmodified-Since-Version cannot "
            msg += "be applied to the same request"
            raise HTTPBadRequest(msg)

        version = self._get_resource_version(request)
        request.response.headers["X-Last-Modified-Version"] = str(version)

        if modified is not None:
            try:
                modified = int(modified)
            except ValueError:
                msg = "Bad value for X-If-Modified-Since-Version: %r"
                raise HTTPBadRequest(msg % (modified,))

            if version <= modified:
                raise HTTPNotModified()

        if unmodified is not None:
            try:
                unmodified = int(unmodified)
            except ValueError:
                msg = 'Invalid value for "X-If-Unmodified-Since-Version": %r'
                raise HTTPBadRequest(msg % (unmodified,))

            if version > unmodified:
                raise HTTPPreconditionFailed()

    def _get_resource_version(self, request):
        """Get last-modified version for the target resource of the request.

        This method retreives the last-modified version of the storage
        itself, a specific collection in the storage, or a specific item
        in a collection, depending on what resouce is targeted by the request.
        If the target resource does not exist, it returns zero.
        """
        storage = self._get_storage(request)
        user_id = request.user["uid"]
        collection = request.matchdict.get("collection")
        # No collection name; return overall storage version.
        if collection is None:
            return storage.get_storage_version(user_id)
        item_id = request.matchdict.get("item")
        # No item id; return version of whole collection.
        if item_id is None:
            try:
                return storage.get_collection_version(user_id, collection)
            except NotFoundError:
                return 0
        # Otherwise, return version of specific item.
        try:
            return storage.get_item_version(user_id, collection, item_id)
        except NotFoundError:
            return 0

    def get_storage(self, request):
        # XXX returns a 400 if the root is called
        raise HTTPBadRequest()

    @convert_storage_errors
    def get_collection_versions(self, request):
        """Returns a hash of collections associated with the account,
        Along with the last modified version for each collection
        """
        request.headers.pop("X-If-Unmodified-Since-Version", None)
        self._check_precondition_headers(request)
        storage = self._get_storage(request)
        user_id = request.user["uid"]
        collections = storage.get_collection_versions(user_id)
        return collections

    @convert_storage_errors
    def get_collection_counts(self, request):
        """Returns a hash of collections associated with the account,
        Along with the total number of items for each collection.
        """
        request.headers.pop("X-If-Unmodified-Since-Version", None)
        self._check_precondition_headers(request)
        storage = self._get_storage(request)
        user_id = request.user["uid"]
        counts = storage.get_collection_counts(user_id)
        return counts

    @convert_storage_errors
    def get_quota(self, request):
        request.headers.pop("X-If-Unmodified-Since-Version", None)
        self._check_precondition_headers(request)
        storage = self._get_storage(request)
        user_id = request.user["uid"]
        used = storage.get_total_size(user_id)
        return {
            "usage": used,
            "quota": self.quota_size,
        }

    @convert_storage_errors
    def get_collection_usage(self, request):
        request.headers.pop("X-If-Unmodified-Since-Version", None)
        self._check_precondition_headers(request)
        storage = self._get_storage(request)
        user_id = request.user["uid"]
        return storage.get_collection_sizes(user_id)

    def _convert_args(self, kw):
        """Converts incoming arguments for GET and DELETE on collections.

        This function will also raise a 400 on bad args.
        Unknown args are just dropped.
        """
        args = {}

        for arg in ('older', 'newer'):
            value = kw.get(arg)
            if value is None:
                continue
            try:
                value = int(value)
            except ValueError:
                msg = 'Invalid value for "%s": %r' % (arg, value)
                raise HTTPBadRequest(msg)
            args[arg] = value

        # convert limit
        limit = kw.get('limit')
        if limit is not None:
            try:
                limit = int(limit)
                if limit <= 0:
                    raise ValueError
            except ValueError:
                msg = 'Invalid value for "limit": %r' % (limit,)
                raise HTTPBadRequest(msg)
            args['limit'] = limit

        # extract offset
        offset = kw.get('offset')
        if offset is not None:
            args['offset'] = offset

        # split comma-separates list of ids.
        ids = kw.get('ids')
        if ids is not None:
            args['items'] = ids.split(',')
            if len(args['items']) > MAX_IDS_PER_BATCH:
                msg = 'Cannot specify more than %s BSO ids at a time'
                raise HTTPBadRequest(msg % (MAX_IDS_PER_BATCH,))

        # validate sort
        sort = kw.get('sort')
        if sort in ('oldest', 'newest', 'index'):
            args['sort'] = sort
        return args

    @convert_storage_errors
    @with_read_lock
    def get_collection(self, request, **kw):
        """Returns a list of the BSO ids contained in a collection."""
        self._check_precondition_headers(request)

        filters = self._convert_args(kw)
        collection_name = request.matchdict['collection']
        user_id = request.user["uid"]
        full = kw.get('full', False)

        storage = self._get_storage(request)
        if full:
            res = storage.get_items(user_id, collection_name, **filters)
        else:
            res = storage.get_item_ids(user_id, collection_name, **filters)

        request.response.headers['X-Num-Records'] = str(len(res["items"]))
        next_offset = res.get("next_offset")
        if next_offset is not None:
            request.response.headers["X-Next-Offset"] = str(next_offset)
        return {
            "items": res["items"]
        }

    @convert_storage_errors
    @with_read_lock
    def get_item(self, request):
        """Returns a single BSO object."""
        self._check_precondition_headers(request)
        storage = self._get_storage(request)
        collection_name = request.matchdict['collection']
        item_id = request.matchdict['item']
        user_id = request.user["uid"]
        return storage.get_item(user_id, collection_name, item_id)

    def _check_quota(self, request, new_bsos):
        """Checks the quota.

        If the quota is reached, issues a 400.
        """
        user_id = request.user["uid"]
        storage = self._get_storage(request)
        if self.quota_size is None:
            return 0

        used = storage.get_total_size(user_id)
        left = self.quota_size - used
        if left < _ONE_MEG:
            used = storage.get_total_size(user_id, recalculate=True)
            left = self.quota_size - used

        for bso in new_bsos:
            left -= len(bso.get("payload", ""))

        if left <= 0:  # no space left
            raise HTTPJsonBadRequest(ERROR_OVER_QUOTA)
        return left

    @convert_storage_errors
    @with_write_lock
    def set_item(self, request):
        """Sets a single BSO object."""
        self._check_precondition_headers(request)

        storage = self._get_storage(request)
        user_id = request.user["uid"]
        collection_name = request.matchdict['collection']
        item_id = request.matchdict['item']

        content_type = request.content_type
        if request.content_type not in ("application/json", None):
            msg = "Unsupported Media Type: %s" % (content_type,)
            raise HTTPUnsupportedMediaType(msg)

        try:
            data = json.loads(request.body)
        except ValueError:
            raise HTTPJsonBadRequest(ERROR_MALFORMED_JSON)

        try:
            bso = BSO(data)
        except ValueError:
            raise HTTPJsonBadRequest(ERROR_INVALID_OBJECT)

        consistent, msg = bso.validate()
        if not consistent:
            raise HTTPJsonBadRequest(ERROR_INVALID_OBJECT)

        left = self._check_quota(request, [bso])
        res = storage.set_item(user_id, collection_name, item_id, bso)

        if not res["created"]:
            response = HTTPNoContent()
        else:
            response = HTTPCreated()

        response.headers["X-Last-Modified-Version"] = str(res["version"])
        if 0 < left < _ONE_MEG:
            response.headers['X-Quota-Remaining'] = str(left)
        return response

    @convert_storage_errors
    @with_write_lock
    def delete_item(self, request):
        """Deletes a single BSO object."""
        self._check_precondition_headers(request)
        storage = self._get_storage(request)
        collection_name = request.matchdict['collection']
        item_id = request.matchdict['item']
        user_id = request.user["uid"]
        storage.delete_item(user_id, collection_name, item_id)
        return HTTPNoContent()

    @convert_storage_errors
    @with_write_lock
    def set_collection(self, request):
        """Sets a batch of BSO objects into a collection."""
        self._check_precondition_headers(request)

        storage = self._get_storage(request)
        user_id = request.user["uid"]
        collection_name = request.matchdict['collection']

        # TODO: it would be lovely to support streaming uploads here...
        content_type = request.content_type
        try:
            if content_type in ("application/json", None):
                bsos = json.loads(request.body)
            elif content_type == "application/newlines":
                bsos = [json.loads(ln) for ln in request.body.split("\n")]
            else:
                msg = "Unsupported Media Type: %s" % (content_type,)
                raise HTTPUnsupportedMediaType(msg)
        except ValueError:
            raise HTTPJsonBadRequest(ERROR_MALFORMED_JSON)

        if not isinstance(bsos, (tuple, list)):
            raise HTTPJsonBadRequest(ERROR_INVALID_OBJECT)

        res = {'success': [], 'failed': {}}

        # Sanity-check each of the BSOs.
        # Limit the batch based on both count and payload size.
        kept_bsos = []
        total_bytes = 0
        for count, bso in enumerate(bsos):
            try:
                bso = BSO(bso)
            except ValueError:
                res['failed'][''] = ['invalid bso']
                continue

            if 'id' not in bso:
                res['failed'][''] = ['invalid id']
                continue

            consistent, msg = bso.validate()
            item_id = bso['id']
            if not consistent:
                res['failed'][item_id] = [msg]
                continue

            if count >= self.batch_max_count:
                res['failed'][item_id] = ['retry bso']
                continue
            if 'payload' in bso:
                total_bytes += len(bso['payload'])
            if total_bytes >= self.batch_max_bytes:
                res['failed'][item_id] = ['retry bytes']
                continue

            kept_bsos.append(bso)

        left = self._check_quota(request, kept_bsos)

        try:
            version = storage.set_items(user_id, collection_name, kept_bsos)
        except Exception, e:
            # Something went wrong.
            # We want to swallow the 503 in that case.
            self.logger.error('Could not set items')
            self.logger.error(str(e))
            for bso in kept_bsos:
                res['failed'][bso['id']] = "db error"
        else:
            res['success'].extend([bso['id'] for bso in kept_bsos])
            request.response.headers["X-Last-Modified-Version"] = str(version)

        if 0 < left < _ONE_MEG:
            request.response.headers['X-Quota-Remaining'] = str(left)
        return res

    @convert_storage_errors
    @with_write_lock
    def delete_collection(self, request, **kw):
        """Deletes the collection and all contents.

        Additional request parameters may modify the selection of which
        items to delete.
        """
        self._check_precondition_headers(request)

        ids = kw.get("ids")
        if ids is not None:
            ids = ids.split(",")
            if len(ids) > MAX_IDS_PER_BATCH:
                msg = 'Cannot delete more than %s BSOs at a time'
                raise HTTPBadRequest(msg % (MAX_IDS_PER_BATCH,))

        storage = self._get_storage(request)
        collection_name = request.matchdict['collection']
        user_id = request.user["uid"]
        if ids is None:
            storage.delete_collection(user_id, collection_name)
            response = HTTPNoContent()
        else:
            version = storage.delete_items(user_id, collection_name, ids)
            response = HTTPNoContent()
            response.headers["X-Last-Modified-Version"] = str(version)
        return response

    @convert_storage_errors
    def delete_storage(self, request):
        """Deletes all records for the user."""
        self._check_precondition_headers(request)
        storage = self._get_storage(request)
        user_id = request.user["uid"]
        storage.delete_storage(user_id)  # XXX failures ?
        return HTTPNoContent()
