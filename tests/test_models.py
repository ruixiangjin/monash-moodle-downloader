import json

from monash_moodle_downloader.models import (
    Activity,
    ActivityType,
    Course,
    Resource,
    ResourceStatus,
    Section,
    SyncManifest,
)


def test_manifest_is_versioned_and_json_serialisable() -> None:
    resource = Resource(
        source_id="resource-7",
        name="notes.pdf",
        source_url="https://learning.monash.edu/mod/resource/view.php?id=7",
        source_kind="moodle_resource",
        status=ResourceStatus.DOWNLOADED,
        local_path="Week 01/Files/notes.pdf",
    )
    activity = Activity(
        id=7,
        name="Lecture notes",
        activity_type=ActivityType.FILE,
        section_id=3,
        resources=[resource],
    )
    course = Course(
        id=44553,
        code="FIT2014",
        name="Theory of Computation",
        sections=[Section(id=3, number=1, title="Week 1", activities=[activity])],
    )

    data = SyncManifest(course=course, resources=[resource]).to_dict()
    encoded = json.dumps(data)

    assert data["schema_version"] == 1
    assert data["course"]["code"] == "FIT2014"
    assert '"downloaded"' in encoded
    assert "cookie" not in encoded.lower()
    assert "sesskey" not in encoded.lower()
