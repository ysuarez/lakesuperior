import logging

from abc import ABCMeta
from collections import defaultdict
from itertools import accumulate, groupby
from pprint import pformat
from uuid import uuid4

import arrow

from flask import current_app, g, request
from rdflib import Graph
from rdflib.resource import Resource
from rdflib.namespace import RDF
from rdflib.term import URIRef, Literal

from lakesuperior.dictionaries.namespaces import ns_collection as nsc
from lakesuperior.dictionaries.srv_mgd_terms import  srv_mgd_subjects, \
        srv_mgd_predicates, srv_mgd_types
from lakesuperior.exceptions import *
from lakesuperior.model.ldp_factory import LdpFactory


def atomic(fn):
    '''
    Handle atomic operations in an RDF store.

    This wrapper ensures that a write operation is performed atomically. It
    also takes care of sending a message for each resource changed in the
    transaction.
    '''
    def wrapper(self, *args, **kwargs):
        request.changelog = []
        try:
            ret = fn(self, *args, **kwargs)
        except:
            self._logger.warn('Rolling back transaction.')
            self.rdfly.store.rollback()
            raise
        else:
            self._logger.info('Committing transaction.')
            self.rdfly.store.commit()
            for ev in request.changelog:
                #self._logger.info('Message: {}'.format(pformat(ev)))
                self._send_event_msg(*ev)
            return ret

    return wrapper



class Ldpr(metaclass=ABCMeta):
    '''LDPR (LDP Resource).

    Definition: https://www.w3.org/TR/ldp/#ldpr-resource

    This class and related subclasses contain the implementation pieces of
    the vanilla LDP specifications. This is extended by the
    `lakesuperior.fcrepo.Resource` class.

    Inheritance graph: https://www.w3.org/TR/ldp/#fig-ldpc-types

    Note: Even though LdpNr (which is a subclass of Ldpr) handles binary files,
    it still has an RDF representation in the triplestore. Hence, some of the
    RDF-related methods are defined in this class rather than in the LdpRs
    class.

    Convention notes:

    All the methods in this class handle internal UUIDs (URN). Public-facing
    URIs are converted from URNs and passed by these methods to the methods
    handling HTTP negotiation.

    The data passed to the store layout for processing should be in a graph.
    All conversion from request payload strings is done here.
    '''

    EMBED_CHILD_RES_URI = nsc['fcrepo'].EmbedResources
    FCREPO_PTREE_TYPE = nsc['fcrepo'].Pairtree
    INS_CNT_REL_URI = nsc['ldp'].insertedContentRelation
    MBR_RSRC_URI = nsc['ldp'].membershipResource
    MBR_REL_URI = nsc['ldp'].hasMemberRelation
    RETURN_CHILD_RES_URI = nsc['fcrepo'].Children
    RETURN_INBOUND_REF_URI = nsc['fcrepo'].InboundReferences
    RETURN_SRV_MGD_RES_URI = nsc['fcrepo'].ServerManaged
    ROOT_NODE_URN = nsc['fcsystem'].root

    # Workflow type. Inbound means that the resource is being written to the
    # store, outbounnd is being retrieved for output.
    WRKF_INBOUND = '_workflow:inbound_'
    WRKF_OUTBOUND = '_workflow:outbound_'

    # Default user to be used for the `createdBy` and `lastUpdatedBy` if a user
    # is not provided.
    DEFAULT_USER = Literal('BypassAdmin')

    RES_CREATED = '_create_'
    RES_DELETED = '_delete_'
    RES_UPDATED = '_update_'

    RES_VER_CONT_LABEL = 'fcr:versions'

    base_types = {
        nsc['fcrepo'].Resource,
        nsc['ldp'].Resource,
        nsc['ldp'].RDFSource,
    }

    protected_pred = (
        nsc['fcrepo'].created,
        nsc['fcrepo'].createdBy,
        nsc['ldp'].contains,
    )

    _logger = logging.getLogger(__name__)


    ## MAGIC METHODS ##

    def __init__(self, uuid, repr_opts={}, provided_imr=None, **kwargs):
        '''Instantiate an in-memory LDP resource that can be loaded from and
        persisted to storage.

        Persistence is done in this class. None of the operations in the store
        layout should commit an open transaction. Methods are wrapped in a
        transaction by using the `@atomic` decorator.

        @param uuid (string) UUID of the resource. If None (must be explicitly
        set) it refers to the root node. It can also be the full URI or URN,
        in which case it will be converted.
        @param repr_opts (dict) Options used to retrieve the IMR. See
        `parse_rfc7240` for format details.
        @Param provd_rdf (string) RDF data provided by the client in
        operations isuch as `PUT` or `POST`, serialized as a string. This sets
        the `provided_imr` property.
        '''
        self.uuid = g.tbox.uri_to_uuid(uuid) \
                if isinstance(uuid, URIRef) else uuid
        self.urn = nsc['fcres'][uuid] \
                if self.uuid else self.ROOT_NODE_URN
        self.uri = g.tbox.uuid_to_uri(self.uuid)

        self.rdfly = current_app.rdfly
        self.nonrdfly = current_app.nonrdfly

        self.provided_imr = provided_imr


    @property
    def rsrc(self):
        '''
        The RDFLib resource representing this LDPR. This is a live
        representation of the stored data if present.

        @return rdflib.resource.Resource
        '''
        if not hasattr(self, '_rsrc'):
            self._rsrc = self.rdfly.ds.resource(self.urn)

        return self._rsrc


    @property
    def imr(self):
        '''
        Extract an in-memory resource from the graph store.

        If the resource is not stored (yet), a `ResourceNotExistsError` is
        raised.

        @return rdflib.resource.Resource
        '''
        if not hasattr(self, '_imr'):
            if hasattr(self, '_imr_options'):
                #self._logger.debug('IMR options: {}'.format(self._imr_options))
                imr_options = self._imr_options
            else:
                imr_options = {}
            options = dict(imr_options, strict=True)
            self._imr = self.rdfly.extract_imr(self.urn, **options)

        return self._imr


    @imr.setter
    def imr(self, v):
        '''
        Replace in-memory buffered resource.

        @param v (set | rdflib.Graph) New set of triples to populate the IMR
        with.
        '''
        if isinstance(v, Resource):
            v = v.graph
        self._imr = Resource(Graph(), self.urn)
        gr = self._imr.graph
        gr += v


    @imr.deleter
    def imr(self):
        '''
        Delete in-memory buffered resource.
        '''
        delattr(self, '_imr')


    @property
    def stored_or_new_imr(self):
        '''
        Extract an in-memory resource for harmless manipulation and output.

        If the resource is not stored (yet), initialize a new IMR with basic
        triples.

        @return rdflib.resource.Resource
        '''
        if not hasattr(self, '_imr'):
            if hasattr(self, '_imr_options'):
                #self._logger.debug('IMR options: {}'.format(self._imr_options))
                imr_options = self._imr_options
            else:
                imr_options = {}
            options = dict(imr_options, strict=True)
            try:
                self._imr = self.rdfly.extract_imr(self.urn, **options)
            except ResourceNotExistsError:
                self._imr = Resource(Graph(), self.urn)
                for t in self.base_types:
                    self.imr.add(RDF.type, t)

        return self._imr


    @property
    def out_graph(self):
        '''
        Retun a globalized graph of the resource's IMR.

        Internal URNs are replaced by global URIs using the endpoint webroot.
        '''
        out_gr = Graph()

        for t in self.imr.graph:
            if (
                # Exclude digest hash and version information.
                t[1] not in {
                    nsc['premis'].hasMessageDigest,
                    nsc['fcrepo'].hasVersion,
                }
            ) and (
                # Only include server managed triples if requested.
                self._imr_options.get('incl_srv_mgd', True)
                or (
                    not t[1] in srv_mgd_predicates
                    and not (t[1] == RDF.type or t[2] in srv_mgd_types)
                )
            ):
                out_gr.add(t)

        return out_gr


    @property
    def version_info(self):
        '''
        Return version metadata (`fcr:versions`).
        '''
        if not hasattr(self, '_version_info'):
            try:
                self._version_info = self.rdfly.get_version_info(self.urn)
            except ResourceNotExistsError as e:
                self._version_info = Graph()

        return self._version_info


    @property
    def versions(self):
        '''
        Return a generator of version URIs.
        '''
        return set(self.version_info[self.urn : nsc['fcrepo'].hasVersion :])


    @property
    def version_uids(self):
        '''
        Return a generator of version UIDs (relative to their parent resource).
        '''
        gen = self.version_info[
            self.urn
            : nsc['fcrepo'].hasVersion / nsc['fcrepo'].hasVersionLabel
            :]

        return { str(uid) for uid in gen }


    @property
    def is_stored(self):
        if not hasattr(self, '_is_stored'):
            if hasattr(self, '_imr'):
                self._is_stored = len(self.imr.graph) > 0
            else:
                self._is_stored = self.rdfly.ask_rsrc_exists(self.urn)

        return self._is_stored


    @property
    def types(self):
        '''All RDF types.

        @return set(rdflib.term.URIRef)
        '''
        if not hasattr(self, '_types'):
            if hasattr(self, 'imr') and len(self.imr.graph):
                imr = self.imr
            elif hasattr(self, 'provided_imr') and \
                    len(self.provided_imr.graph):
                imr = provided_imr

            self._types = set(imr.graph[self.urn : RDF.type])

        return self._types


    @property
    def ldp_types(self):
        '''The LDP types.

        @return set(rdflib.term.URIRef)
        '''
        if not hasattr(self, '_ldp_types'):
            self._ldp_types = { t for t in self.types if nsc['ldp'] in t }

        return self._ldp_types


    ## LDP METHODS ##

    def head(self):
        '''
        Return values for the headers.
        '''
        out_headers = defaultdict(list)

        digest = self.imr.value(nsc['premis'].hasMessageDigest)
        if digest:
            etag = digest.identifier.split(':')[-1]
            out_headers['ETag'] = 'W/"{}"'.format(etag),

        last_updated_term = self.imr.value(nsc['fcrepo'].lastModified)
        if last_updated_term:
            out_headers['Last-Modified'] = arrow.get(last_updated_term)\
                .format('ddd, D MMM YYYY HH:mm:ss Z')

        for t in self.ldp_types:
            out_headers['Link'].append(
                    '{};rel="type"'.format(t.n3()))

        return out_headers


    def get(self):
        '''
        This gets the RDF metadata. The binary retrieval is handled directly
        by the route.
        '''
        return g.tbox.globalize_graph(self.out_graph)


    @atomic
    def post(self):
        '''
        https://www.w3.org/TR/ldp/#ldpr-HTTP_POST

        Perform a POST action after a valid resource URI has been found.
        '''
        return self._create_or_replace_rsrc(create_only=True)


    @atomic
    def put(self):
        '''
        https://www.w3.org/TR/ldp/#ldpr-HTTP_PUT
        '''
        return self._create_or_replace_rsrc()


    def patch(self, *args, **kwargs):
        raise NotImplementedError()


    @atomic
    def delete(self, inbound=True, delete_children=True, leave_tstone=True):
        '''
        https://www.w3.org/TR/ldp/#ldpr-HTTP_DELETE

        @param inbound (boolean) If specified, delete all inbound relationships
        as well. This is the default and is always the case if referential
        integrity is enforced by configuration.
        @param delete_children (boolean) Whether to delete all child resources.
        This is the default.
        '''
        refint = current_app.config['store']['ldp_rs']['referential_integrity']
        inbound = True if refint else inbound

        children = self.imr[nsc['ldp'].contains * '+'] \
                if delete_children else []

        if leave_tstone:
            ret = self._bury_rsrc(inbound)
        else:
            ret = self._purge_rsrc(inbound)

        for child_uri in children:
            child_rsrc = LdpFactory.from_stored(
                g.tbox.uri_to_uuid(child_uri.identifier),
                repr_opts={'incl_children' : False})
            if leave_tstone:
                child_rsrc._bury_rsrc(inbound, tstone_pointer=self.urn)
            else:
                child_rsrc._purge_rsrc(inbound)

        return ret


    @atomic
    def resurrect(self):
        '''
        Resurrect a resource from a tombstone.

        @EXPERIMENTAL
        '''
        tstone_trp = set(self.rdfly.extract_imr(self.urn, strict=False).graph)

        ver_rsp = self.version_info.query('''
        SELECT ?uid {
          ?latest fcrepo:hasVersionLabel ?uid ;
            fcrepo:created ?ts .
        }
        ORDER BY DESC(?ts)
        LIMIT 1
        ''')
        ver_uid = str(ver_rsp.bindings[0]['uid'])
        ver_trp = set(self.rdfly.get_version(self.urn, ver_uid))

        laz_gr = Graph()
        for t in ver_trp:
            if t[1] != RDF.type or t[2] not in {
                nsc['fcrepo'].Version,
            }:
                laz_gr.add((self.urn, t[1], t[2]))
        laz_gr.add((self.urn, RDF.type, nsc['fcrepo'].Resource))
        if nsc['ldp'].NonRdfSource in laz_gr[: RDF.type :]:
            laz_gr.add((self.urn, RDF.type, nsc['fcrepo'].Binary))
        elif nsc['ldp'].Container in laz_gr[: RDF.type :]:
            laz_gr.add((self.urn, RDF.type, nsc['fcrepo'].Container))

        self._modify_rsrc(self.RES_CREATED, tstone_trp, set(laz_gr))
        self._set_containment_rel()

        return self.uri



    @atomic
    def purge(self, inbound=True):
        '''
        Delete a tombstone and all historic snapstots.

        N.B. This does not trigger an event.
        '''
        refint = current_app.config['store']['ldp_rs']['referential_integrity']
        inbound = True if refint else inbound

        return self._purge_rsrc(inbound)


    def get_version_info(self):
        '''
        Get the `fcr:versions` graph.
        '''
        return g.tbox.globalize_graph(self.version_info)


    def get_version(self, ver_uid):
        '''
        Get a version by label.
        '''
        ver_gr = self.rdfly.get_version(self.urn, ver_uid)

        return g.tbox.globalize_graph(ver_gr)


    @atomic
    def create_version(self, ver_uid):
        '''
        Create a new version of the resource.

        NOTE: This creates an event only for the resource being updated (due
        to the added `hasVersion` triple and possibly to the `hasVersions` one)
        but not for the version being created.

        @param ver_uid Version ver_uid. If already existing, an exception is
        raised.
        '''
        if not ver_uid or ver_uid in self.version_uids:
            ver_uid = str(uuid4())

        return g.tbox.globalize_term(self._create_rsrc_version(ver_uid))


    @atomic
    def revert_to_version(self, ver_uid, backup=True):
        '''
        Revert to a previous version.

        NOTE: this will create a new version.

        @param ver_uid (string) Version UID.
        @param backup (boolean) Whether to create a backup copy. Default is
        true.
        '''
        # Create a backup snapshot.
        if backup:
            self.create_version(uuid4())

        ver_gr = self.rdfly.get_version(self.urn, ver_uid)
        revert_gr = Graph()
        for t in ver_gr:
            if t[1] not in srv_mgd_predicates and not(
                t[1] == RDF.type and t[2] in srv_mgd_types
            ):
                revert_gr.add((self.urn, t[1], t[2]))

        self.provided_imr = revert_gr.resource(self.urn)

        return self._create_or_replace_rsrc(create_only=False)


    ## PROTECTED METHODS ##

    def _create_or_replace_rsrc(self, create_only=False):
        '''
        Create or update a resource. PUT and POST methods, which are almost
        identical, are wrappers for this method.

        @param create_only (boolean) Whether this is a create-only operation.
        '''
        create = create_only or not self.is_stored

        self._add_srv_mgd_triples(create)
        self._ensure_single_subject_rdf(self.provided_imr.graph)
        ref_int = self.rdfly.config['referential_integrity']
        if ref_int:
            self._check_ref_int(ref_int)

        if create:
            ev_type = self._create_rsrc()
        else:
            ev_type = self._replace_rsrc()

        self._set_containment_rel()

        return ev_type


    def _create_rsrc(self):
        '''
        Create a new resource by comparing an empty graph with the provided
        IMR graph.
        '''
        self._modify_rsrc(self.RES_CREATED, add_trp=self.provided_imr.graph)

        # Set the IMR contents to the "add" triples.
        self.imr = self.provided_imr.graph

        return self.RES_CREATED


    def _replace_rsrc(self):
        '''
        Replace a resource.

        The existing resource graph is removed except for the protected terms.
        '''
        # The extracted IMR is used as a "minus" delta, so protected predicates
        # must be removed.
        for p in self.protected_pred:
            self.imr.remove(p)

        delta = self._dedup_deltas(self.imr.graph, self.provided_imr.graph)
        self._modify_rsrc(self.RES_UPDATED, *delta)

        # Set the IMR contents to the "add" triples.
        self.imr = delta[1]

        return self.RES_UPDATED


    def _bury_rsrc(self, inbound, tstone_pointer=None):
        '''
        Delete a single resource and create a tombstone.

        @param inbound (boolean) Whether to delete the inbound relationships.
        @param tstone_pointer (URIRef) If set to a URN, this creates a pointer
        to the tombstone of the resource that used to contain the deleted
        resource. Otherwise the deleted resource becomes a tombstone.
        '''
        self._logger.info('Removing resource {}'.format(self.urn))
        # Create a backup snapshot for resurrection purposes.
        self._create_rsrc_version(uuid4())

        remove_trp = self.imr.graph
        add_trp = Graph()

        if tstone_pointer:
            add_trp.add((self.urn, nsc['fcsystem'].tombstone,
                    tstone_pointer))
        else:
            add_trp.add((self.urn, RDF.type, nsc['fcsystem'].Tombstone))
            add_trp.add((self.urn, nsc['fcrepo'].created, g.timestamp_term))

        self._modify_rsrc(self.RES_DELETED, remove_trp, add_trp)

        if inbound:
            for ib_rsrc_uri in self.imr.graph.subjects(None, self.urn):
                remove_trp = {(ib_rsrc_uri, None, self.urn)}
                Ldpr(ib_rsrc_uri)._modify_rsrc(self.RES_UPDATED, remove_trp)

        return self.RES_DELETED


    def _purge_rsrc(self, inbound):
        '''
        Remove all traces of a resource and versions.
        '''
        self._logger.info('Purging resource {}'.format(self.urn))
        imr = self.rdfly.extract_imr(
                self.urn, incl_inbound=True, strict=False)

        # Remove resource itself.
        self.rdfly.modify_dataset({(self.urn, None, None)}, types=None)

        # Remove fragments.
        for frag_urn in imr.graph[
                : nsc['fcsystem'].fragmentOf : self.urn]:
            self.rdfly.modify_dataset({(frag_urn, None, None)}, types={})

        # Remove snapshots.
        for snap_urn in self.versions:
            remove_trp = {
                (snap_urn, None, None),
                (None, None, snap_urn),
            }
            self.rdfly.modify_dataset(remove_trp, types={})

        # Remove inbound references.
        if inbound:
            for ib_rsrc_uri in imr.graph.subjects(None, self.urn):
                remove_trp = {(ib_rsrc_uri, None, self.urn)}
                Ldpr(ib_rsrc_uri)._modify_rsrc(self.RES_UPDATED, remove_trp)

        # @TODO This could be a different event type.
        return self.RES_DELETED


    def _create_rsrc_version(self, ver_uid):
        '''
        Perform version creation and return the internal URN.
        '''
        # Create version resource from copying the current state.
        ver_add_gr = Graph()
        vers_uuid = '{}/{}'.format(self.uuid, self.RES_VER_CONT_LABEL)
        ver_uuid = '{}/{}'.format(vers_uuid, ver_uid)
        ver_urn = nsc['fcres'][ver_uuid]
        ver_add_gr.add((ver_urn, RDF.type, nsc['fcrepo'].Version))
        for t in self.imr.graph:
            if (
                t[1] == RDF.type and t[2] in {
                    nsc['fcrepo'].Binary,
                    nsc['fcrepo'].Container,
                    nsc['fcrepo'].Resource,
                }
            ) or (
                t[1] in {
                    nsc['fcrepo'].hasParent,
                    nsc['fcrepo'].hasVersions,
                    nsc['premis'].hasMessageDigest,
                }
            ):
                pass
            else:
                ver_add_gr.add((
                        g.tbox.replace_term_domain(t[0], self.urn, ver_urn),
                        t[1], t[2]))

        self.rdfly.modify_dataset(
                add_trp=ver_add_gr, types={nsc['fcrepo'].Version})

        # Add version metadata.
        meta_add_gr = Graph()
        meta_add_gr.add((
            self.urn, nsc['fcrepo'].hasVersion, ver_urn))
        meta_add_gr.add(
                (ver_urn, nsc['fcrepo'].created, g.timestamp_term))
        meta_add_gr.add(
                (ver_urn, nsc['fcrepo'].hasVersionLabel, Literal(ver_uid)))

        self.rdfly.modify_dataset(
                add_trp=meta_add_gr, types={nsc['fcrepo'].Metadata})

        # Update resource.
        rsrc_add_gr = Graph()
        rsrc_add_gr.add((
            self.urn, nsc['fcrepo'].hasVersions, nsc['fcres'][vers_uuid]))

        self._modify_rsrc(self.RES_UPDATED, add_trp=rsrc_add_gr, notify=False)

        return nsc['fcres'][ver_uuid]


    def _modify_rsrc(self, ev_type, remove_trp=Graph(), add_trp=Graph(),
                     notify=True):
        '''
        Low-level method to modify a graph for a single resource.

        This is a crucial point for messaging. Any write operation on the RDF
        store that needs to be notified should be performed by invoking this
        method.

        @param ev_type (string) The type of event (create, update, delete).
        @param remove_trp (rdflib.Graph) Triples to be removed.
        @param add_trp (rdflib.Graph) Triples to be added.
        '''
        # If one of the triple sets is not a graph, do a set merge and
        # filtering. This is necessary to support non-RDF terms (e.g.
        # variables).
        if not isinstance(remove_trp, Graph) or not isinstance(add_trp, Graph):
            if isinstance(remove_trp, Graph):
                remove_trp = set(remove_trp)
            if isinstance(add_trp, Graph):
                add_trp = set(add_trp)
            merge_gr = remove_trp | add_trp
            type = { trp[2] for trp in merge_gr if trp[1] == RDF.type }
            actor = { trp[2] for trp in merge_gr \
                    if trp[1] == nsc['fcrepo'].createdBy }
        else:
            merge_gr = remove_trp | add_trp
            type = merge_gr[self.urn : RDF.type]
            actor = merge_gr[self.urn : nsc['fcrepo'].createdBy]

        ret = self.rdfly.modify_dataset(remove_trp, add_trp)

        if notify and current_app.config.get('messaging'):
            request.changelog.append((set(remove_trp), set(add_trp), {
                'ev_type' : ev_type,
                'time' : g.timestamp,
                'type' : type,
                'actor' : actor,
            }))

        return ret


    def _ensure_single_subject_rdf(self, gr, add_fragment=True):
        '''
        Ensure that a RDF payload for a POST or PUT has a single resource.
        '''
        for s in set(gr.subjects()):
            # Fragment components
            if '#' in s:
                parts = s.split('#')
                frag = s
                s = URIRef(parts[0])
                if add_fragment:
                    # @TODO This is added to the main graph. It should be added
                    # to the metadata graph.
                    gr.add((frag, nsc['fcsystem'].fragmentOf, s))
            if not s == self.urn:
                raise SingleSubjectError(s, self.uuid)


    def _check_ref_int(self, config):
        gr = self.provided_imr.graph

        for o in gr.objects():
            if isinstance(o, URIRef) and str(o).startswith(g.webroot)\
                    and not self.rdfly.ask_rsrc_exists(o):
                if config == 'strict':
                    raise RefIntViolationError(o)
                else:
                    self._logger.info(
                            'Removing link to non-existent repo resource: {}'
                            .format(o))
                    gr.remove((None, None, o))


    def _check_mgd_terms(self, gr):
        '''
        Check whether server-managed terms are in a RDF payload.
        '''
        # @FIXME Need to be more consistent
        if getattr(self, 'handling', 'none') == 'none':
            return gr

        offending_subjects = set(gr.subjects()) & srv_mgd_subjects
        if offending_subjects:
            if self.handling=='strict':
                raise ServerManagedTermError(offending_subjects, 's')
            else:
                for s in offending_subjects:
                    self._logger.info('Removing offending subj: {}'.format(s))
                    gr.remove((s, None, None))

        offending_predicates = set(gr.predicates()) & srv_mgd_predicates
        if offending_predicates:
            if self.handling=='strict':
                raise ServerManagedTermError(offending_predicates, 'p')
            else:
                for p in offending_predicates:
                    self._logger.info('Removing offending pred: {}'.format(p))
                    gr.remove((None, p, None))

        offending_types = set(gr.objects(predicate=RDF.type)) & srv_mgd_types
        if offending_types:
            if self.handling=='strict':
                raise ServerManagedTermError(offending_types, 't')
            else:
                for t in offending_types:
                    self._logger.info('Removing offending type: {}'.format(t))
                    gr.remove((None, RDF.type, t))

        #self._logger.debug('Sanitized graph: {}'.format(gr.serialize(
        #    format='turtle').decode('utf-8')))
        return gr


    def _add_srv_mgd_triples(self, create=False):
        '''
        Add server-managed triples to a provided IMR.

        @param create (boolean) Whether the resource is being created.
        '''
        # Base LDP types.
        for t in self.base_types:
            self.provided_imr.add(RDF.type, t)

        # Message digest.
        cksum = g.tbox.rdf_cksum(self.provided_imr.graph)
        self.provided_imr.set(nsc['premis'].hasMessageDigest,
                URIRef('urn:sha1:{}'.format(cksum)))

        # Create and modify timestamp.
        if create:
            self.provided_imr.set(nsc['fcrepo'].created, g.timestamp_term)
            self.provided_imr.set(nsc['fcrepo'].createdBy, self.DEFAULT_USER)

        self.provided_imr.set(nsc['fcrepo'].lastModified, g.timestamp_term)
        self.provided_imr.set(nsc['fcrepo'].lastModifiedBy, self.DEFAULT_USER)


    def _set_containment_rel(self):
        '''Find the closest parent in the path indicated by the UUID and
        establish a containment triple.

        E.g. if only urn:fcres:a (short: a) exists:
        - If a/b/c/d is being created, a becomes container of a/b/c/d. Also,
          pairtree nodes are created for a/b and a/b/c.
        - If e is being created, the root node becomes container of e.
        '''
        if self.urn == self.ROOT_NODE_URN:
            return
        elif '/' in self.uuid:
            # Traverse up the hierarchy to find the parent.
            parent_uri = self._find_parent_or_create_pairtree()
        else:
            parent_uri = self.ROOT_NODE_URN

        add_gr = Graph()
        add_gr.add((parent_uri, nsc['ldp'].contains, self.urn))
        parent_rsrc = LdpFactory.from_stored(
                g.tbox.uri_to_uuid(parent_uri), repr_opts={
                'incl_children' : False}, handling='none')
        parent_rsrc._modify_rsrc(self.RES_UPDATED, add_trp=add_gr)

        # Direct or indirect container relationship.
        self._add_ldp_dc_ic_rel(parent_rsrc)


    def _find_parent_or_create_pairtree(self):
        '''
        Check the path-wise parent of the new resource. If it exists, return
        its URI. Otherwise, create pairtree resources up the path until an
        actual resource or the root node is found.

        @return rdflib.term.URIRef
        '''
        path_components = self.uuid.split('/')

         # If there is only on element, the parent is the root node.
        if len(path_components) < 2:
            return self.ROOT_NODE_URN

        # Build search list, e.g. for a/b/c/d/e would be a/b/c/d, a/b/c, a/b, a
        self._logger.info('Path components: {}'.format(path_components))
        fwd_search_order = accumulate(
            list(path_components)[:-1],
            func=lambda x,y : x + '/' + y
        )
        rev_search_order = reversed(list(fwd_search_order))

        cur_child_uri = nsc['fcres'][self.uuid]
        parent_uri = None
        segments = []
        for cparent_uuid in rev_search_order:
            cparent_uri = nsc['fcres'][cparent_uuid]

            if self.rdfly.ask_rsrc_exists(cparent_uri):
                parent_uri = cparent_uri
                break
            else:
                segments.append((cparent_uri, cur_child_uri))
                cur_child_uri = cparent_uri

        if parent_uri is None:
            parent_uri = self.ROOT_NODE_URN

        for uri, child_uri in segments:
            self._create_path_segment(uri, child_uri, parent_uri)

        return parent_uri


    def _dedup_deltas(self, remove_gr, add_gr):
        '''
        Remove duplicate triples from add and remove delta graphs, which would
        otherwise contain unnecessary statements that annul each other.
        '''
        return (
            remove_gr - add_gr,
            add_gr - remove_gr
        )


    def _create_path_segment(self, uri, child_uri, real_parent_uri):
        '''
        Create a path segment with a non-LDP containment statement.

        This diverges from the default fcrepo4 behavior which creates pairtree
        resources.

        If a resource such as `fcres:a/b/c` is created, and neither fcres:a or
        fcres:a/b exists, we have to create two "hidden" containment statements
        between a and a/b and between a/b and a/b/c in order to maintain the
        `containment chain.
        '''
        imr = Resource(Graph(), uri)
        imr.add(RDF.type, nsc['ldp'].Container)
        imr.add(RDF.type, nsc['ldp'].BasicContainer)
        imr.add(RDF.type, nsc['ldp'].RDFSource)
        imr.add(RDF.type, nsc['fcrepo'].Pairtree)
        imr.add(nsc['fcrepo'].contains, child_uri)
        imr.add(nsc['ldp'].contains, self.urn)
        imr.add(nsc['fcrepo'].hasParent, real_parent_uri)

        # If the path segment is just below root
        if '/' not in str(uri):
            imr.graph.add((nsc['fcsystem'].root, nsc['fcrepo'].contains, uri))

        self.rdfly.modify_dataset(add_trp=imr.graph)


    def _add_ldp_dc_ic_rel(self, cont_rsrc):
        '''
        Add relationship triples from a parent direct or indirect container.

        @param cont_rsrc (rdflib.resource.Resouce)  The container resource.
        '''
        cont_p = set(cont_rsrc.imr.graph.predicates())
        add_gr = Graph()

        self._logger.info('Checking direct or indirect containment.')
        #self._logger.debug('Parent predicates: {}'.format(cont_p))

        add_gr.add((self.urn, nsc['fcrepo'].hasParent, cont_rsrc.urn))
        if self.MBR_RSRC_URI in cont_p and self.MBR_REL_URI in cont_p:
            s = g.tbox.localize_term(
                    cont_rsrc.imr.value(self.MBR_RSRC_URI).identifier)
            p = cont_rsrc.imr.value(self.MBR_REL_URI).identifier

            if cont_rsrc.imr[RDF.type : nsc['ldp'].DirectContainer]:
                self._logger.info('Parent is a direct container.')

                self._logger.debug('Creating DC triples.')
                add_gr.add((s, p, self.urn))

            elif cont_rsrc.imr[RDF.type : nsc['ldp'].IndirectContainer] \
                   and self.INS_CNT_REL_URI in cont_p:
                self._logger.info('Parent is an indirect container.')
                cont_rel_uri = cont_rsrc.imr.value(self.INS_CNT_REL_URI).identifier
                target_uri = self.provided_imr.value(cont_rel_uri).identifier
                self._logger.debug('Target URI: {}'.format(target_uri))
                if target_uri:
                    self._logger.debug('Creating IC triples.')
                    add_gr.add((s, p, target_uri))

        if len(add_gr):
            #add_gr = self._check_mgd_terms(add_gr)
            #self._logger.debug('Adding DC/IC triples: {}'.format(
            #    add_gr.serialize(format='turtle').decode('utf-8')))
            self._modify_rsrc(self.RES_UPDATED, add_trp=add_gr)


    def _send_event_msg(self, remove_trp, add_trp, metadata):
        '''
        Break down delta triples, find subjects and send event message.
        '''
        remove_grp = groupby(remove_trp, lambda x : x[0])
        remove_dict = { k[0] : k[1] for k in remove_grp }

        add_grp = groupby(add_trp, lambda x : x[0])
        add_dict = { k[0] : k[1] for k in add_grp }

        subjects = set(remove_dict.keys()) | set(add_dict.keys())
        for rsrc_uri in subjects:
            self._logger.info('subject: {}'.format(rsrc_uri))
            #current_app.messenger.send
