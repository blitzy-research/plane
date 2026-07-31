# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import pytest
from rest_framework import status
from uuid import uuid4

from plane.db.models import (
    Issue,
    IssueActivity,
    IssueAssignee,
    IssueComment,
    IssueLabel,
    Label,
    Project,
    ProjectMember,
    State,
)


@pytest.fixture
def project(db, workspace, create_user):
    """Create a test project with the user as a member"""
    project = Project.objects.create(
        name="Test Project",
        identifier="TP",
        workspace=workspace,
        created_by=create_user,
    )
    ProjectMember.objects.create(
        project=project,
        member=create_user,
        role=20,  # Admin role
        is_active=True,
    )
    return project


@pytest.fixture
def source_issue(db, project, create_user):
    """Create the work item to duplicate, carrying a priority, one assignee, one label and a completed state.

    ``completed_at`` is never written by hand. The work item is attached to a state whose group is
    ``completed`` so the model's own ``_sync_completed_at`` populates the column during ``save()``,
    which is the only way a real work item ever acquires that value. That makes the clone's
    ``completed_at`` assertions meaningful: the duplicate endpoint copies the state, so the column
    would be repopulated on insert unless the endpoint explicitly normalizes it afterwards.
    """
    completed_state = State.objects.create(
        name="Completed State",
        color="#16A34A",
        group="completed",
        project=project,
        workspace=project.workspace,
        created_by=create_user,
    )
    label = Label.objects.create(
        name="Source Label",
        color="#FF5733",
        description="A label on the work item being duplicated",
        project=project,
        workspace=project.workspace,
        created_by=create_user,
    )
    issue = Issue.objects.create(
        name="Source Work Item",
        description_html="<p>Source work item description</p>",
        priority="high",
        state=completed_state,
        project=project,
        workspace=project.workspace,
        created_by=create_user,
    )
    IssueAssignee.objects.create(
        issue=issue,
        assignee=create_user,
        project=project,
        workspace=project.workspace,
        created_by=create_user,
    )
    IssueLabel.objects.create(
        issue=issue,
        label=label,
        project=project,
        workspace=project.workspace,
        created_by=create_user,
    )
    # Reload so the synchronizer populated ``completed_at`` and the save assigned ``sequence_id``
    # are visible to the assertions that compare the clone against this source.
    issue.refresh_from_db()
    return issue


@pytest.mark.contract
class TestWorkItemDuplicate:
    """Test Work Item Duplicate API Endpoint"""

    def get_duplicate_url(self, workspace_slug, project_id, pk):
        """Helper to get the work item duplicate endpoint URL"""
        return f"/api/v1/workspaces/{workspace_slug}/projects/{project_id}/work-items/{pk}/duplicate/"

    @pytest.mark.django_db
    def test_duplicate_work_item_success(self, api_key_client, workspace, project, source_issue):
        """Test successful work item duplication"""
        url = self.get_duplicate_url(workspace.slug, project.id, source_issue.id)

        # Derive the expected many-to-many payloads by reading the through tables exactly as the
        # serializer renders them, so no identifier is hard coded. The fixture creates one row of
        # each, so both lists are non-empty and the equality assertions below cannot pass vacuously.
        # The endpoint never touches the source's rows, so these values stay valid across the call.
        expected_assignees = sorted(
            str(assignee_id)
            for assignee_id in IssueAssignee.objects.filter(issue=source_issue).values_list("assignee_id", flat=True)
        )
        expected_labels = sorted(
            str(label_id)
            for label_id in IssueLabel.objects.filter(issue=source_issue).values_list("label_id", flat=True)
        )

        response = api_key_client.post(url)

        assert response.status_code == status.HTTP_201_CREATED

        # ``id`` is rendered by a PrimaryKeyRelatedField and arrives as a UUID object, while the
        # many-to-many sets are injected as strings. Both sides of every identifier comparison are
        # stringified so the assertions hold regardless of how the field is rendered.
        assert str(response.data["id"]) != str(source_issue.id)
        assert str(response.data["sequence_id"]) != str(source_issue.sequence_id)
        assert response.data["name"].endswith(" (Copy)")
        assert response.data["priority"] == source_issue.priority
        assert sorted(response.data["assignees"]) == expected_assignees
        assert sorted(response.data["labels"]) == expected_labels
        assert response.data["completed_at"] is None

        # Re-fetching the persisted row proves the post-write normalization reached the database
        # rather than only the serialized payload, even though the clone copies a completed state.
        duplicated_issue = Issue.objects.get(pk=response.data["id"])
        assert duplicated_issue.completed_at is None

        # The clone starts with a clean history: no comment is copied and no activity is dispatched.
        assert IssueComment.objects.filter(issue=duplicated_issue).count() == 0
        assert IssueActivity.objects.filter(issue=duplicated_issue).count() == 0

    @pytest.mark.django_db
    def test_duplicate_work_item_not_found(self, api_key_client, workspace, project):
        """Test duplicating a non-existent work item"""
        fake_id = uuid4()
        url = self.get_duplicate_url(workspace.slug, project.id, fake_id)

        response = api_key_client.post(url)
        assert response.status_code == status.HTTP_404_NOT_FOUND
