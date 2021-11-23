#-----------------------------------------------------------------------------
# Copyright (c) 2012 - 2018, Anaconda, Inc. and Intake contributors
# All rights reserved.
#
# The full license is in the LICENSE file, distributed with this software.
#-----------------------------------------------------------------------------

import collections
import copy
import keyword
import logging
import posixpath
import re
import warnings

import msgpack

from ..source import registry as plugin_registry
from . import Catalog
from .entry import CatalogEntry
from .utils import expand_defaults, coerce, RemoteCatalogError
from ..compat import unpack_kwargs, pack_kwargs
from ..utils import remake_instance
from intake.auth.base import BaseClientAuth, AuthenticationFailure
logger = logging.getLogger('intake')


class RemoteCatalog(Catalog):
    """The state of a remote Intake server"""
    name = 'intake_remote'

    def __init__(self, url, http_args=None, page_size=None,
                 name=None, source_id=None, metadata=None, auth=None, ttl=1,
                 getenv=True, getshell=True,
                 storage_options=None, parameters=None, persist_mode="default"):
        """Connect to remote Intake Server as a catalog

        Parameters
        ----------
        url: str
            Address of the server, e.g., "intake://localhost:5000".
        http_args: dict
            Arguments to add to HTTP calls, including "ssl" (True/False) for
            secure connections.
        page_size : int, optional
            The number of entries fetched at a time during iteration.
            Default is None (no pagination; fetch all entries in bulk).
        name : str, optional
            Unique identifier for catalog. This is primarily useful when
            manually constructing a catalog. Defaults to None.
        source_id : str, optional
            Emphemeral unique ID generated by the server, if known.
        metadata: dict
            Additional information about this data
        auth : BaseClientAuth or None
            Default, None, falls back to BaseClientAuth.
        ttl : float, optional
            Lifespan (time to live) of cached modification time. Units are in
            seconds. Defaults to 1.
        getenv: bool
            Can parameter default fields take values from the environment
        getshell: bool
            Can parameter default fields run shell commands
        storage_options : dict
            If using a URL beginning with 'intake://' (remote Intake server),
            parameters to pass to requests when issuing http commands; otherwise
            parameters to pass to remote backend file-system. Ignored for
            normal local files.
        parameters: dict
            To pass to the server when it instantiates the data source
        """
        from requests.compat import urljoin, urlparse
        if http_args is None:
            http_args = {}
        else:
            # Make a deep copy to avoid mutating input.
            http_args = copy.deepcopy(http_args)
        secure = http_args.pop('ssl', False)
        scheme = 'https' if secure else 'http'
        url = url.replace('intake', scheme, 1)
        if not url.endswith('/'):
            url = url + '/'
        self.url = url
        self.info_url = urljoin(url, 'v1/info')
        self.source_url = urljoin(url, 'v1/source')
        self.http_args = http_args
        self.http_args.update(storage_options or {})
        self.http_args['headers'] = self.http_args.get('headers', {})
        self._page_size = page_size
        self._source_id = source_id
        self._parameters = parameters
        self._len = None
        self.auth = auth or BaseClientAuth()

        if self._source_id is None:
            name = urlparse(url).netloc.replace(
                '.', '_').replace(':', '_')
        super(RemoteCatalog, self).__init__(
            name=name, metadata=name, ttl=ttl, getenv=getenv,
            getshell=getshell, storage_options=storage_options, persist_mode=persist_mode)

    def _make_entries_container(self):
        return Entries(self)

    def __dir__(self):
        # Include (cached) tab-completable entries and normal attributes.
        return (
            [key for key in self._ipython_key_completions_() if
             re.match("[_A-Za-z][_a-zA-Z0-9]*$", key)  # valid Python identifier
             and not keyword.iskeyword(key)]  # not a Python keyword
            + list(self.__dict__.keys()))

    def _ipython_key_completions_(self):
        if not self._entries.complete:
            # Ensure that at least one page of data has been loaded so that
            # *some* entries are included.
            next(iter(self))
        if not self._entries.complete:
            warnings.warn(
                "Tab-complete and dir() on RemoteCatalog may include only a "
                "subset of the available entries.")
        # Loop through the cached entries, but do not trigger iteration over
        # the full set.
        # Intentionally access _entries directly to avoid paying for a reload.
        return [key for key, _ in self._entries.cached_items()]

    @property
    def page_size(self):
        return self._page_size

    def fetch_page(self, page_offset):
        import requests
        logger.debug("Request page entries %d-%d",
                     page_offset, page_offset + self._page_size)
        params = {'page_offset': page_offset,
                  'page_size': self._page_size}
        http_args = self._get_http_args(params)
        response = requests.get(self.info_url, **http_args)
        # Produce a chained exception with both the underlying HTTPError
        # and our own more direct context.
        try:
            response.raise_for_status()
        except requests.HTTPError as err:
            raise RemoteCatalogError(
                "Failed to fetch page of entries {}-{}."
                "".format(page_offset, page_offset + self._page_size)) from err
        info = msgpack.unpackb(response.content, **unpack_kwargs)
        page = {}
        for source in info['sources']:
            user_parameters = source.get('user_parameters', [])
            # TODO Do something with self._parameters.
            page[source['name']] = RemoteCatalogEntry(
                url=self.url,
                getenv=self.getenv,
                getshell=self.getshell,
                auth=self.auth,
                http_args=self.http_args,
                page_size=self._page_size,
                persist_mode=self.pmode,
                # user_parameters=user_parameters,
                **source)
        return page

    def fetch_by_name(self, name):
        import requests
        logger.debug("Requesting info about entry named '%s'", name)
        params = {'name': name}
        http_args = self._get_http_args(params)
        response = requests.get(self.source_url, **http_args)
        if response.status_code == 404:
            raise KeyError(name)
        try:
            response.raise_for_status()
        except requests.HTTPError as err:
            raise RemoteCatalogError(
                "Failed to fetch entry {!r}.".format(name)) from err
        info = msgpack.unpackb(response.content, **unpack_kwargs)
        return RemoteCatalogEntry(
            url=self.url,
            getenv=self.getenv,
            getshell=self.getshell,
            auth=self.auth,
            http_args=self.http_args,
            page_size=self._page_size,
            persist_mode=self.pmode,
            **info['source'])

    def _get_http_args(self, params):
        """
        Return a copy of the http_args

        Adds auth headers and 'source-id', merges in params.
        """
        # Add the auth headers to any other headers
        headers = self.http_args.get('headers', {})
        if self.auth is not None:
            auth_headers = self.auth.get_headers()
            headers.update(auth_headers)

        # build new http args with these headers
        http_args = self.http_args.copy()
        if self._source_id is not None:
            headers['source-id'] = self._source_id
        http_args['headers'] = headers

        # Merge in any params specified by the caller.
        merged_params = http_args.get('params', {})
        merged_params.update(params)
        http_args['params'] = merged_params
        return http_args

    def _load(self):
        """Fetch metadata from remote. Entries are fetched lazily."""
        # This will not immediately fetch any sources (entries). It will lazily
        # fetch sources from the server in paginated blocks when this Catalog
        # is iterated over. It will fetch specific sources when they are
        # accessed in this Catalog via __getitem__.
        import requests

        if self.page_size is None:
            # Fetch all source info.
            params = {}
        else:
            # Just fetch the metadata now; fetch source info later in pages.
            params = {'page_offset': 0, 'page_size': 0}
        http_args = self._get_http_args(params)
        response = requests.get(self.info_url, **http_args)
        try:
            response.raise_for_status()
            error = False
        except requests.HTTPError as err:
            if '403' in err.args[0]:
                error = "Your current level of authentication does not have access"
            else:
                raise RemoteCatalogError(
                    "Failed to fetch metadata.") from err
        if error:
            raise AuthenticationFailure(error)
        info = msgpack.unpackb(response.content, **unpack_kwargs)
        self.metadata = info['metadata']
        # The intake server now always provides a length, but the server may be
        # running an older version of intake.
        self._len = info.get('length')
        self._entries.reset()
        # If we are paginating (page_size is not None) and the server we are
        # working with is new enough to support pagination, info['sources']
        # should be empty. If either of those things is not true,
        # info['sources'] will contain all the entries and we should cache them
        # now.
        if info['sources']:
            # Signal that we are not paginating, even if we were asked to.
            self._page_size = None
            self._entries._page_cache.update(
                {source['name']: RemoteCatalogEntry(
                    url=self.url,
                    getenv=self.getenv,
                    getshell=self.getshell,
                    auth=self.auth,
                    http_args=self.http_args, **source)
                 for source in info['sources']})

    def search(self, *args, **kwargs):
        import requests
        request = {'action': 'search', 'query': (args, kwargs),
                   'source_id': self._source_id}
        response = requests.post(
            url=self.source_url, **self._get_http_args({}),
            data=msgpack.packb(request, **pack_kwargs))
        try:
            response.raise_for_status()
        except requests.HTTPError as err:
            raise RemoteCatalogError("Failed search query.") from err
        source = msgpack.unpackb(response.content, **unpack_kwargs)
        source_id = source['source_id']
        cat = RemoteCatalog(
            url=self.url,
            http_args=self.http_args,
            source_id=source_id,
            persist_mode=self.pmode,
            name="")
        cat.cat = self
        return cat

    def __len__(self):
        if self._len is None:
            # The server is running an old version of intake and did not
            # provide a length, so we have no choice but to do this the
            # expensive way.
            return sum(1 for _ in self)
        else:
            return self._len

    @staticmethod
    def _persist(source, path, **kwargs):
        return RemoteCatalog._data_to_source(source, path, **kwargs)

    @staticmethod
    def _data_to_source(cat, path, **kwargs):
        from intake.catalog.local import YAMLFileCatalog
        from fsspec import open_files
        import yaml
        if not isinstance(cat, Catalog):
            raise NotImplementedError
        out = {}
        # reach down into the private state because we apparently need the
        # Entry here rather than the public facing DataSource objects.
        for name, entry in cat._entries.items():
            out[name] = entry.__getstate__()
            out[name]['parameters'] = [up._captured_init_kwargs for up
                                       in entry._user_parameters]
            out[name]['kwargs'].pop('parameters')
        fn = posixpath.join(path, 'cat.yaml')
        with open_files([fn], 'wt')[0] as f:
            yaml.dump({'sources': out}, f)
        return YAMLFileCatalog(fn)


class Entries(collections.abc.Mapping):
    """Fetches entries from server on item lookup and iteration.

    This fetches pages of entries from the server during iteration and
    caches them. On __getitem__ it fetches the specific entry from the
    server.
    """
    # This has PY3-style lazy methods (keys, values, items). Since it's
    # internal we should not need the PY2-only iter* variants.
    def __init__(self, catalog):
        self._catalog = catalog
        self._page_cache = collections.OrderedDict()
        # Put lookups that were due to __getitem__ in a separate cache
        # so that iteration reflects the server's order, not an
        # arbitrary cache order.
        self._direct_lookup_cache = {}
        self._page_offset = 0
        # True if all pages are cached locally
        self.complete = self._catalog.page_size is None

    def reset(self):
        "Clear caches to force a reload."
        self._page_cache.clear()
        self._direct_lookup_cache.clear()
        self._page_offset = 0
        self.complete = self._catalog.page_size is None

    def __iter__(self):
        for key in self._page_cache:
            yield key
        if self._catalog.page_size is None:
            # We are not paginating, either because the user set page_size=None
            # or the server is a version of intake before pagination parameters
            # were added.
            return
        # Fetch more entries from the server.
        while True:
            page = self._catalog.fetch_page(self._page_offset)
            self._page_cache.update(page)
            self._page_offset += len(page)
            for key in page:
                yield key
            if len(page) < self._catalog.page_size:
                # Partial or empty page.
                # We are done until the next call to items(), when we
                # will resume at the offset where we left off.
                self.complete = True
                break

    def cached_items(self):
        """
        Iterate over items that are already cached. Perform no requests.
        """
        for item in self._page_cache.items():
            yield item
        for item in self._direct_lookup_cache.items():
            yield item

    def __getitem__(self, key):
        try:
            return self._direct_lookup_cache[key]
        except KeyError:
            try:
                return self._page_cache[key]
            except KeyError:
                source = self._catalog.fetch_by_name(key)
                self._direct_lookup_cache[key] = source
                return source

    def __len__(self):
        return len(self._catalog)


class RemoteCatalogEntry(CatalogEntry):
    """An entry referring to a remote data definition"""
    def __init__(self, url, auth, name=None, user_parameters=None,
                 container=None, description='', metadata=None,
                 http_args=None, page_size=None, persist_mode="default", direct_access=False,
                 getenv=True, getshell=True, **kwargs):
        """

        Parameters
        ----------
        url: str
            HTTP address of the Intake server this entry comes from
        auth: Auth instance
            If there are additional headers to add to calls, this instance will
            provide them
        kwargs: additional keys describing the entry, name, description,
            container,
        """
        self.url = url
        if isinstance(auth, dict):
            auth = remake_instance(auth)
        self.auth = auth
        self.container = container
        self.name = name
        self.description = description
        self._metadata = metadata or {}
        self._page_size = page_size
        # Persist mode describing a nested RemoteCatalog
        self.catalog_pmode = persist_mode
        self._user_parameters = [remake_instance(up)
                                 if (isinstance(up, dict) and 'cls' in up)
                                 else up
                                 for up in user_parameters or []]
        self._direct_access = direct_access
        self.http_args = (http_args or {}).copy()
        if 'headers' not in self.http_args:
            self.http_args['headers'] = {}
        
        super(RemoteCatalogEntry, self).__init__(getenv=getenv,
                                                 getshell=getshell)

        # Persist mode for the RemoteCatalogEntry
        self._pmode = "never"

    def describe(self):
        return {
            'name': self.name,
            'container': self.container,
            'plugin': "remote",
            'description': self.description,
            'direct_access': self._direct_access,
            'metadata': self._metadata,
            'user_parameters': self._user_parameters,
            'args': (self.url, )
        }

    def get(self, **user_parameters):
        for par in self._user_parameters:
            if par['name'] not in user_parameters:
                default = par['default']
                if isinstance(default, str):
                    default = coerce(par['type'], expand_defaults(
                        par['default'], True, self.getenv, self.getshell))
                user_parameters[par['name']] = default

        http_args = self.http_args.copy()
        http_args['headers'] = self.http_args['headers'].copy()
        http_args['headers'].update(self.auth.get_headers())
        return open_remote(
            self.url, self.name, container=self.container,
            user_parameters=user_parameters, description=self.description,
            http_args=http_args,
            page_size=self._page_size,
            auth=self.auth,
            getenv=self.getenv,
            persist_mode=self.catalog_pmode,
            getshell=self.getshell)


def open_remote(url, entry, container, user_parameters, description, http_args,
                page_size=None, persist_mode=None, auth=None, getenv=None, getshell=None):
    """Create either local direct data source or remote streamed source"""
    from intake.container import container_map
    import msgpack
    import requests
    from requests.compat import urljoin

    if url.startswith('intake://'):
        url = url[len('intake://'):]
    payload = dict(action='open',
                   name=entry,
                   parameters=user_parameters,
                   available_plugins=list(plugin_registry.keys()))
    req = requests.post(urljoin(url, '/v1/source'),
                        data=msgpack.packb(payload, **pack_kwargs),
                        **http_args)
    if req.ok:
        response = msgpack.unpackb(req.content, **unpack_kwargs)

        if 'plugin' in response:
            pl = response['plugin']
            pl = [pl] if isinstance(pl, str) else pl
            # Direct access
            for p in pl:
                if p in plugin_registry:
                    source = plugin_registry[p](**response['args'])
                    proxy = False
                    break
            else:
                proxy = True
        else:
            proxy = True
        if proxy:
            response.pop('container')
            response.update({'name': entry, 'parameters': user_parameters})
            if container == 'catalog':
                response.update({'auth': auth,
                                 'getenv': getenv,
                                 'getshell': getshell,
                                 'page_size': page_size,
                                 'persist_mode': persist_mode
                                 # TODO ttl?
                                 # TODO storage_options?
                                 })
            source = container_map[container](url, http_args, **response)
        source.description = description
        return source
    else:
        raise Exception('Server error: %d, %s' % (req.status_code, req.reason))

