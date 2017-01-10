import logging
import six

from globus_sdk import exc
from globus_sdk.response import GlobusResponse
from globus_sdk.transfer.response import IterableTransferResponse

logger = logging.getLogger(__name__)


class PaginatedResource(GlobusResponse, six.Iterator):
    """
    A class that describes paginated Transfer API resources.
    This is not a top level helper func because it depends upon the pagination
    implementation of the Transfer API, which may not be the implementation
    chosen by other, future APIs.

    Expectations about Paginated Transfer API Resources:
    - They support `limit` and `offset` query params, with `limit` being a
      count of elements to return, and offset being an offset into the result
      set (as opposed to a page number), 0-based
      OR
      They support a `marker` query param, which is an opaque value
    - They return a JSON result with `has_next_page` as a boolean key,
      indicating whether or not there are more results available -- even if the
      hard limit for the API forbids requesting these results
      OR
      They return a JSON result with `marker` as a key indicating whether or
      not there are more results available
    - Individual results are JSON objects inside of an array named `DATA` in
      the returned JSON document

    This is a class and not a function because it needs to enforce distinct
    actions between initialization and iteration. If defined as a generator
    function with the `yield` syntax, python won't let us distinguish between
    creating the iterable object and iterating it.
    To eagerly trigger errors from the first call, we need to wrap it up in a
    class.
    """

    # pages have 'has_next_page', 'offset', and 'limit'
    PAGING_STYLE_HAS_NEXT = 0
    # pages have 'offset', 'limit', and 'total'
    PAGING_STYLE_TOTAL = 1
    # pages have a 'has_next_page', but use 'last_key' rather than 'offset' +
    # 'limit'
    PAGING_STYLE_LAST_KEY = 2
    # pages have a 'marker' attribute, which refers to the "next page" of
    # results, but which is opaque
    PAGING_STYLE_MARKER = 3

    # bind an object at class def time to act as a sentinel value for iteration
    # Basically grabbing something that can't be duplicated by iteration
    # results
    _magic = object()

    def __init__(self,
                 # passthrough stuff for making a TransferClient method call
                 client_method, path, client_kwargs,
                 # paging parameters
                 num_results=10, max_results_per_call=1000,
                 max_total_results=None, offset=0,
                 paging_style=PAGING_STYLE_HAS_NEXT):
        """
        Takes a TransferClient method, a selection of its arguments, a variety
        of limits on result sizes, an offest into the result set, and a
        "paging style", which defines which kind of Transfer paging behavior
        we'll see.

        max_results_per_call and max_total_results are fairly self-descriptive.
        They are limits imposed by the API, but which the SDK must be aware of
        and respect.
        This also takes a number of results to return, `num_results`. It isn't
        immediately obvious why num_results and max_total_results aren't one in
        the same, so to explain: num_results is the number of results the user
        requested. max_total_results is a limit on the number of results that
        can be requested. The caller could handle this, but it's nice and
        uniform to put the num results >? max_total_results check in here.

        Offset is an offset into the result set, and is only applicable if
        paging style is HAS_KEY or TOTAL.
        """
        logger.info("Creating PaginatedResource({}) on {}(instance:{}):{}:{}"
                    .format(paging_style,
                            client_method.__self__.__class__.__name__,
                            id(client_method.__self__),
                            client_method.__name__, path))
        self.num_results = num_results
        self.max_results_per_call = max_results_per_call
        self.max_total_results = max_total_results
        self.offset = offset
        self.paging_style = paging_style

        # counter for how many results we've gotten thusfar, used to cap paging
        # in non-offset based styles
        self.num_results_fetched = 0

        # potentially necessary params during paging
        self.limit = None
        self.next_marker = None

        # check the requested num results to see if it exceeds the maximum
        # total number of results allowed by the API
        # only check if there is a max_total_results though
        if (self.max_total_results is not None and
                num_results > self.max_total_results):
            logger.error(("PaginatedResource would overrun limits set by API. "
                          "Please request a lower num_results"))
            raise exc.PaginationOverrunError((
                'Paginated call would exceed API limit. Pass a smaller '
                'num_results parameter -- the maximum for this call is {0}')
                .format(self.max_total_results))

        # what function call does this class instance wrap up?
        self.client_method = client_method
        self.client_path = path
        self.client_kwargs = client_kwargs
        self.client_kwargs['response_class'] = IterableTransferResponse

        # convert the iterable_func method into a generator expression by
        # calling it
        self.generator = self.iterable_func()

        # grab the first element out of the internal iteration function
        # because this could raise a StopIteration exception, we need to be
        # careful and make sure that such a condition is respected (and
        # replicated as an iterable of length 0)
        try:
            self.first_elem = next(self.generator)
        except StopIteration:
            # express this internally as "generator is null" -- just need some
            # way of making sure that it's clear
            self.generator = None

    @property
    def data(self):
        """
        To get the "data" on a PaginatedResource, fetch all pages and convert
        them into the only python data structure that makes sense: a list.
        """
        return list(self)

    def __iter__(self):
        """
        Each instance is an iterable, so make it the result of `__iter__` and
        rely on an explicit `next()` method.
        """
        return self

    def __next__(self):
        """
        PaginatedResource objects are iterable collections of results from an
        underlying function. However, they have special behavior when being
        setup, which is where the magical `first_elem` comes into play,
        capturing the first iteration result.
        """
        # if the generator was empty from the start, just raise a StopIteration
        # here and now
        if self.generator is None:
            logger.debug(("PaginatedResource never got results, "
                          "iteration empty (not an error!)"))
            raise StopIteration()

        if self.first_elem != self._magic:
            tmp = self.first_elem
            self.first_elem = self._magic
            return tmp
        else:
            return next(self.generator)

    def iterable_func(self):
        """
        An internal function which has generator semantics. Defined using the
        `yield` syntax.
        Used to grab the first element during class initialization, and
        subsequently on calls to `next()` to get the remaining elements.
        We rely on the implicit StopIteration built into this type of function
        to propagate through the final `next()` call.

        This method is the real workhorse of this entire module.
        """
        if not self.client_kwargs['params']:
            self.client_kwargs['params'] = {}

        # to start with, cap the limit per request to the max per request size
        self.limit = min(self.num_results, self.max_results_per_call)

        def _set_params_for_next_call():
            # if we're about to request more results than the user asked
            # for, limit ourselves on the last paginated call to the API
            if self.offset + self.limit > self.num_results:
                self.limit = self.num_results - self.offset

            # all paging styles support limit
            # MARKER doesn't have it documented, but it is in fact supported
            self.client_kwargs['params']['limit'] = self.limit

            # if the paging is done by marker, just carry over the marker
            if self.paging_style == self.PAGING_STYLE_MARKER:
                if self.next_marker:
                    self.client_kwargs['params']['marker'] = (
                        self.next_marker)
            # these params work for all paging styles *except* MARKER
            else:
                self.client_kwargs['params']['offset'] = self.offset

        def _check_has_next_page(res):
            """
            Check that the API says there are more results available.

            Additionally, update the PaginatedResource.maker or
            PaginatedResource.offset based on the response
            """
            # if the paging style is MARKER, look at the marker
            if self.paging_style == self.PAGING_STYLE_MARKER:
                # marker may be 0, null, or absent if no more results
                # API docs aren't 100% clear -- looks like 0 is what we should
                # expect, but we'll also accept null or absent to be safe
                self.next_marker = res.get('next_marker')
                return bool(self.next_marker)

            # start doing the offset maths and see if we have another page to
            # fetch
            # step size is the number of results per call -- we'll catch this
            # "walking off the end" of the requested results afterwards
            self.offset += self.max_results_per_call

            # if it's HAS_NEXT, the check is easy, as it's explicitly part of
            # the response
            if self.paging_style == self.PAGING_STYLE_HAS_NEXT:
                # just return the has_next_page value
                return res['has_next_page']

            # if paging is TOTAL oriented, check if we've reached the total
            if self.paging_style == self.PAGING_STYLE_TOTAL:
                return self.offset < res['total']

            logger.error("PaginatedResource.paging_style={} is invalid"
                         .format(self.paging_style))
            raise ValueError(
                'Invalid Paging Style Given to PaginatedResource')

        has_next_page = True
        while has_next_page:
            logger.debug(("PaginatedResource should have more results, "
                          "requesting them now"))
            _set_params_for_next_call()

            # fetch a page of results and walk them, yielding them as the
            # iterated elements wrapped in GlobusResponse objects
            # nicely, the __getitem__ for GlobusResponse will work on raw
            # dicts, so these handle well
            res = self.client_method(self.client_path, **self.client_kwargs)
            for item in res:
                yield GlobusResponse(item)
                # increment the "num results" counter
                self.num_results_fetched += 1

                # ensure that even if the paging style requires that we fetch
                # more results than were requested, we still only yield the
                # number that were requested -- returning here will result in a
                # StopIteration because this is a generator function
                # CAREFUL! make sure we catch num_results_fetched==num_results
                # otherwise, we could end up making one-too-many API calls
                if self.num_results_fetched >= self.num_results:
                    return

            has_next_page = _check_has_next_page(res)
