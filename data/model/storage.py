import logging

from peewee import SQL, IntegrityError
from cachetools.func import lru_cache
from collections import namedtuple

from data.model import (
    config,
    db_transaction,
    InvalidImageException,
    TorrentInfoDoesNotExist,
    DataModelException,
    _basequery,
)
from data.database import (
    ImageStorage,
    Image,
    ImageStoragePlacement,
    ImageStorageLocation,
    ImageStorageTransformation,
    ImageStorageSignature,
    ImageStorageSignatureKind,
    Repository,
    Namespace,
    TorrentInfo,
    ApprBlob,
    ensure_under_transaction,
    ManifestBlob,
)

logger = logging.getLogger(__name__)

_Location = namedtuple("location", ["id", "name"])

EMPTY_LAYER_BLOB_DIGEST = "sha256:a3ed95caeb02ffe68cdd9fd84406680ae93d633cb16422d00e8a7c22955b46d4"
SPECIAL_BLOB_DIGESTS = set([EMPTY_LAYER_BLOB_DIGEST])


@lru_cache(maxsize=1)
def get_image_locations():
    location_map = {}
    for location in ImageStorageLocation.select():
        location_tuple = _Location(location.id, location.name)
        location_map[location.id] = location_tuple
        location_map[location.name] = location_tuple

    return location_map


def get_image_location_for_name(location_name):
    locations = get_image_locations()
    return locations[location_name]


def get_image_location_for_id(location_id):
    locations = get_image_locations()
    return locations[location_id]


def add_storage_placement(storage, location_name):
    """
    Adds a storage placement for the given storage at the given location.
    """
    location = get_image_location_for_name(location_name)
    try:
        ImageStoragePlacement.create(location=location.id, storage=storage)
    except IntegrityError:
        # Placement already exists. Nothing to do.
        pass


def _orphaned_storage_query(candidate_ids):
    """
    Returns the subset of the candidate ImageStorage IDs representing storages that are no longer
    referenced by images.
    """
    # Issue a union query to find all storages that are still referenced by a candidate storage. This
    # is much faster than the group_by and having call we used to use here.
    nonorphaned_queries = []
    for counter, candidate_id in enumerate(candidate_ids):
        query_alias = "q{0}".format(counter)

        # TODO: remove the join with Image once fully on the OCI data model.
        storage_subq = (
            ImageStorage.select(ImageStorage.id)
            .join(Image)
            .where(ImageStorage.id == candidate_id)
            .limit(1)
            .alias(query_alias)
        )

        nonorphaned_queries.append(ImageStorage.select(SQL("*")).from_(storage_subq))

        manifest_storage_subq = (
            ImageStorage.select(ImageStorage.id)
            .join(ManifestBlob)
            .where(ImageStorage.id == candidate_id)
            .limit(1)
            .alias(query_alias)
        )

        nonorphaned_queries.append(ImageStorage.select(SQL("*")).from_(manifest_storage_subq))

    # Build the set of storages that are missing. These storages are orphaned.
    nonorphaned_storage_ids = {
        storage.id for storage in _basequery.reduce_as_tree(nonorphaned_queries)
    }
    return list(candidate_ids - nonorphaned_storage_ids)


def garbage_collect_storage(storage_id_whitelist):
    """
    Performs GC on a possible subset of the storage's with the IDs found in the whitelist.

    The storages in the whitelist will be checked, and any orphaned will be removed, with those IDs
    being returned.
    """
    if len(storage_id_whitelist) == 0:
        return []

    def placements_to_filtered_paths_set(placements_list):
        """
        Returns the list of paths to remove from storage, filtered from the given placements query
        by removing any CAS paths that are still referenced by storage(s) in the database.
        """
        with ensure_under_transaction():
            if not placements_list:
                return set()

            # Find the content checksums not referenced by other storages. Any that are, we cannot
            # remove.
            content_checksums = set(
                [
                    placement.storage.content_checksum
                    for placement in placements_list
                    if placement.storage.cas_path
                ]
            )

            unreferenced_checksums = set()
            if content_checksums:
                # Check the current image storage.
                query = ImageStorage.select(ImageStorage.content_checksum).where(
                    ImageStorage.content_checksum << list(content_checksums)
                )
                is_referenced_checksums = set(
                    [image_storage.content_checksum for image_storage in query]
                )
                if is_referenced_checksums:
                    logger.warning(
                        "GC attempted to remove CAS checksums %s, which are still IS referenced",
                        is_referenced_checksums,
                    )

                # Check the ApprBlob table as well.
                query = ApprBlob.select(ApprBlob.digest).where(
                    ApprBlob.digest << list(content_checksums)
                )
                appr_blob_referenced_checksums = set([blob.digest for blob in query])
                if appr_blob_referenced_checksums:
                    logger.warning(
                        "GC attempted to remove CAS checksums %s, which are ApprBlob referenced",
                        appr_blob_referenced_checksums,
                    )

                unreferenced_checksums = (
                    content_checksums - appr_blob_referenced_checksums - is_referenced_checksums
                )

            # Return all placements for all image storages found not at a CAS path or with a content
            # checksum that is referenced.
            return {
                (
                    get_image_location_for_id(placement.location_id).name,
                    get_layer_path(placement.storage),
                    placement.storage.content_checksum,
                )
                for placement in placements_list
                if not placement.storage.cas_path
                or placement.storage.content_checksum in unreferenced_checksums
            }

    # Note: Both of these deletes must occur in the same transaction (unfortunately) because a
    # storage without any placement is invalid, and a placement cannot exist without a storage.
    # TODO: We might want to allow for null storages on placements, which would allow us to
    # delete the storages, then delete the placements in a non-transaction.
    logger.debug("Garbage collecting storages from candidates: %s", storage_id_whitelist)
    with db_transaction():
        orphaned_storage_ids = _orphaned_storage_query(storage_id_whitelist)
        if len(orphaned_storage_ids) == 0:
            # Nothing to GC.
            return []

        placements_to_remove = list(
            ImageStoragePlacement.select(ImageStoragePlacement, ImageStorage)
            .join(ImageStorage)
            .where(ImageStorage.id << orphaned_storage_ids)
        )

        # Remove the placements for orphaned storages
        if len(placements_to_remove) > 0:
            placement_ids_to_remove = [placement.id for placement in placements_to_remove]
            placements_removed = (
                ImageStoragePlacement.delete()
                .where(ImageStoragePlacement.id << placement_ids_to_remove)
                .execute()
            )
            logger.debug("Removed %s image storage placements", placements_removed)

        # Remove all orphaned storages
        torrents_removed = (
            TorrentInfo.delete().where(TorrentInfo.storage << orphaned_storage_ids).execute()
        )
        logger.debug("Removed %s torrent info records", torrents_removed)

        signatures_removed = (
            ImageStorageSignature.delete()
            .where(ImageStorageSignature.storage << orphaned_storage_ids)
            .execute()
        )
        logger.debug("Removed %s image storage signatures", signatures_removed)

        storages_removed = (
            ImageStorage.delete().where(ImageStorage.id << orphaned_storage_ids).execute()
        )
        logger.debug("Removed %s image storage records", storages_removed)

        # Determine the paths to remove. We cannot simply remove all paths matching storages, as CAS
        # can share the same path. We further filter these paths by checking for any storages still in
        # the database with the same content checksum.
        paths_to_remove = placements_to_filtered_paths_set(placements_to_remove)

    # We are going to make the conscious decision to not delete image storage blobs inside
    # transactions.
    # This may end up producing garbage in s3, trading off for higher availability in the database.
    for location_name, image_path, storage_checksum in paths_to_remove:
        if storage_checksum:
            # Skip any specialized blob digests that we know we should keep around.
            if storage_checksum in SPECIAL_BLOB_DIGESTS:
                continue

            # Perform one final check to ensure the blob is not needed.
            try:
                ImageStorage.select().where(ImageStorage.content_checksum == storage_checksum).get()
                continue
            except ImageStorage.DoesNotExist:
                pass

        logger.debug("Removing %s from %s", image_path, location_name)
        config.store.remove({location_name}, image_path)

    return orphaned_storage_ids


def create_v1_storage(location_name):
    storage = ImageStorage.create(cas_path=False, uploading=True)
    location = get_image_location_for_name(location_name)
    ImageStoragePlacement.create(location=location.id, storage=storage)
    storage.locations = {location_name}
    return storage


def find_or_create_storage_signature(storage, signature_kind_name):
    found = lookup_storage_signature(storage, signature_kind_name)
    if found is None:
        kind = ImageStorageSignatureKind.get(name=signature_kind_name)
        found = ImageStorageSignature.create(storage=storage, kind=kind)

    return found


def lookup_storage_signature(storage, signature_kind_name):
    kind = ImageStorageSignatureKind.get(name=signature_kind_name)
    try:
        return (
            ImageStorageSignature.select()
            .where(ImageStorageSignature.storage == storage, ImageStorageSignature.kind == kind)
            .get()
        )
    except ImageStorageSignature.DoesNotExist:
        return None


def _get_storage(query_modifier):
    query = (
        ImageStoragePlacement.select(ImageStoragePlacement, ImageStorage)
        .switch(ImageStoragePlacement)
        .join(ImageStorage)
    )

    placements = list(query_modifier(query))

    if not placements:
        raise InvalidImageException()

    found = placements[0].storage
    found.locations = {
        get_image_location_for_id(placement.location_id).name for placement in placements
    }
    return found


def get_storage_by_uuid(storage_uuid):
    def filter_to_uuid(query):
        return query.where(ImageStorage.uuid == storage_uuid)

    try:
        return _get_storage(filter_to_uuid)
    except InvalidImageException:
        raise InvalidImageException("No storage found with uuid: %s", storage_uuid)


def get_layer_path(storage_record):
    """
    Returns the path in the storage engine to the layer data referenced by the storage row.
    """
    assert storage_record.cas_path is not None
    return get_layer_path_for_storage(
        storage_record.uuid, storage_record.cas_path, storage_record.content_checksum
    )


def get_layer_path_for_storage(storage_uuid, cas_path, content_checksum):
    """
    Returns the path in the storage engine to the layer data referenced by the storage information.
    """
    store = config.store
    if not cas_path:
        logger.debug("Serving layer from legacy v1 path for storage %s", storage_uuid)
        return store.v1_image_layer_path(storage_uuid)

    return store.blob_path(content_checksum)


def lookup_repo_storages_by_content_checksum(repo, checksums, by_manifest=False):
    """
    Looks up repository storages (without placements) matching the given repository and checksum.
    """
    if not checksums:
        return []

    # There may be many duplicates of the checksums, so for performance reasons we are going
    # to use a union to select just one storage with each checksum
    queries = []

    for counter, checksum in enumerate(set(checksums)):
        query_alias = "q{0}".format(counter)

        # TODO: Remove once we have a new-style model for tracking temp uploaded blobs and
        # all legacy tables have been removed.
        if by_manifest:
            candidate_subq = (
                ImageStorage.select(
                    ImageStorage.id,
                    ImageStorage.content_checksum,
                    ImageStorage.image_size,
                    ImageStorage.uuid,
                    ImageStorage.cas_path,
                    ImageStorage.uncompressed_size,
                    ImageStorage.uploading,
                )
                .join(ManifestBlob)
                .where(ManifestBlob.repository == repo, ImageStorage.content_checksum == checksum)
                .limit(1)
                .alias(query_alias)
            )
        else:
            candidate_subq = (
                ImageStorage.select(
                    ImageStorage.id,
                    ImageStorage.content_checksum,
                    ImageStorage.image_size,
                    ImageStorage.uuid,
                    ImageStorage.cas_path,
                    ImageStorage.uncompressed_size,
                    ImageStorage.uploading,
                )
                .join(Image)
                .where(Image.repository == repo, ImageStorage.content_checksum == checksum)
                .limit(1)
                .alias(query_alias)
            )

        queries.append(ImageStorage.select(SQL("*")).from_(candidate_subq))

    return _basequery.reduce_as_tree(queries)


def set_image_storage_metadata(
    docker_image_id, namespace_name, repository_name, image_size, uncompressed_size
):
    """
    Sets metadata that is specific to the binary storage of the data, irrespective of how it is used
    in the layer tree.
    """
    if image_size is None:
        raise DataModelException("Empty image size field")

    try:
        image = (
            Image.select(Image, ImageStorage)
            .join(Repository)
            .join(Namespace, on=(Repository.namespace_user == Namespace.id))
            .switch(Image)
            .join(ImageStorage)
            .where(
                Repository.name == repository_name,
                Namespace.username == namespace_name,
                Image.docker_image_id == docker_image_id,
            )
            .get()
        )
    except ImageStorage.DoesNotExist:
        raise InvalidImageException("No image with specified id and repository")

    # We MUST do this here, it can't be done in the corresponding image call because the storage
    # has not yet been pushed
    image.aggregate_size = _basequery.calculate_image_aggregate_size(
        image.ancestors, image_size, image.parent
    )
    image.save()

    image.storage.image_size = image_size
    image.storage.uncompressed_size = uncompressed_size
    image.storage.save()
    return image.storage


def get_storage_locations(uuid):
    query = ImageStoragePlacement.select().join(ImageStorage).where(ImageStorage.uuid == uuid)

    return [get_image_location_for_id(placement.location_id).name for placement in query]


def save_torrent_info(storage_object, piece_length, pieces):
    try:
        return TorrentInfo.get(storage=storage_object, piece_length=piece_length)
    except TorrentInfo.DoesNotExist:
        try:
            return TorrentInfo.create(
                storage=storage_object, piece_length=piece_length, pieces=pieces
            )
        except IntegrityError:
            # TorrentInfo already exists for this storage.
            return TorrentInfo.get(storage=storage_object, piece_length=piece_length)


def get_torrent_info(blob):
    try:
        return TorrentInfo.select().where(TorrentInfo.storage == blob).get()
    except TorrentInfo.DoesNotExist:
        raise TorrentInfoDoesNotExist
