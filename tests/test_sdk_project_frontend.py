from __future__ import annotations

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from playwright_api import ProjectMemoryScope, ProjectRef
from playwright_api.errors import ConflictingIdentityError
from playwright_api.frontend import create_project_frontend, find_project_frontend


class FakeLocator:
    def __init__(self, *, visible: bool, page: "FakePage", kind: str = "") -> None:
        self.visible = visible
        self.page = page
        self.kind = kind

    @property
    def first(self) -> "FakeLocator":
        return self

    async def wait_for(self, *, state: str, timeout: int) -> None:
        assert state == "visible"
        assert timeout > 0
        if not self.visible:
            raise PlaywrightTimeoutError("not visible")

    async def evaluate(self, script: str) -> None:
        assert script == "element => element.click()"
        if self.kind == "open":
            self.page.modal_open = True
        elif self.kind == "memory-trigger":
            self.page.memory_trigger_clicks += 1
            self.page.memory_menu_open = True
            self.page.events.append("trigger")
        elif self.kind == "memory-option":
            assert self.page.memory_menu_open is True
            self.page.memory_option_clicks += 1
            self.page.memory_scope = ProjectMemoryScope.PROJECT_ONLY
            self.page.events.append("option")
        elif self.kind == "submit":
            assert self.page.boundary_entered is True
            self.page.submit_clicks += 1
            self.page.submitted = True
            self.page.events.append("submit")

    async def fill(self, value: str) -> None:
        assert self.kind == "name"
        self.page.name = value


class FakePage:
    def __init__(self) -> None:
        self.url = "https://chatgpt.com/projects"
        self.modal_open = False
        self.boundary_entered = False
        self.submitted = False
        self.name = ""
        self.memory_menu_open = False
        self.memory_scope = ProjectMemoryScope.DEFAULT
        self.memory_trigger_clicks = 0
        self.memory_option_clicks = 0
        self.submit_clicks = 0
        self.waits: list[int] = []
        self.events: list[str] = []

    async def goto(self, url: str, *, wait_until: str, timeout: int) -> None:
        assert url == "https://chatgpt.com/projects"
        assert wait_until == "domcontentloaded"
        assert timeout == 60_000

    def get_by_text(self, text: str, *, exact: bool):
        raise AssertionError(f"unexpected fuzzy text lookup: {text!r}, exact={exact!r}")

    def locator(self, selector: str) -> FakeLocator:
        if selector == 'button[aria-label="New project"]':
            return FakeLocator(visible=True, page=self, kind="open")
        if selector == 'form[data-testid="create-new-project-form"] input[name="projectName"]':
            return FakeLocator(visible=self.modal_open, page=self, kind="name")
        if selector == '[data-testid="project-memory-scope-trigger"]':
            return FakeLocator(visible=self.modal_open, page=self, kind="memory-trigger")
        if selector == '[data-testid="project-memory-scope-project-only"]':
            return FakeLocator(visible=False, page=self)
        if selector == (
            'button[role="menuitemradio"]:'
            'has([role="heading"]:text-is("Project-only memory"))'
        ):
            return FakeLocator(
                visible=self.modal_open and self.memory_menu_open,
                page=self,
                kind="memory-option",
            )
        if selector == 'form[data-testid="create-new-project-form"] button[type="submit"]':
            return FakeLocator(visible=self.modal_open, page=self, kind="submit")
        return FakeLocator(visible=False, page=self)

    async def wait_for_timeout(self, milliseconds: int) -> None:
        self.waits.append(milliseconds)

    async def wait_for_url(self, pattern: str, *, timeout: int) -> None:
        assert pattern == "**/g/g-p-*/project"
        assert timeout == 60_000
        assert self.submitted is True
        self.url = "https://chatgpt.com/g/g-p-project123/project"


@pytest.mark.asyncio
async def test_create_project_uses_current_stable_form_contract_and_marks_boundary(
    monkeypatch,
) -> None:
    import playwright_api.frontend as frontend_module

    page = FakePage()
    expected = ProjectRef(
        project_id="g-p-project123",
        canonical_url="https://chatgpt.com/g/g-p-project123/project",
        name="Task Project",
        memory_scope=ProjectMemoryScope.DEFAULT,
    )

    async def observed(_page, *, project_id, name):
        assert project_id == expected.project_id
        assert name == expected.name
        return expected

    monkeypatch.setattr(frontend_module, "find_project_frontend", observed)

    def on_boundary() -> None:
        assert page.name == "Task Project"
        page.boundary_entered = True

    project = await create_project_frontend(
        page,  # type: ignore[arg-type]
        name="Task Project",
        memory_scope=ProjectMemoryScope.DEFAULT,
        on_create_boundary=on_boundary,
    )

    assert project == expected
    assert page.boundary_entered is True
    assert page.submitted is True
    assert page.memory_trigger_clicks == 0
    assert page.memory_option_clicks == 0
    assert page.submit_clicks == 1
    assert page.waits == [1_000, 1_000, 1_000, 1_000]


@pytest.mark.asyncio
async def test_create_project_opens_exact_memory_scope_selector_before_project_only(
    monkeypatch,
) -> None:
    import playwright_api.frontend as frontend_module

    page = FakePage()
    exact_option = (
        'button[role="menuitemradio"]:'
        'has([role="heading"]:text-is("Project-only memory"))'
    )
    assert page.locator('[data-testid="project-memory-scope-project-only"]').visible is False
    assert page.locator(exact_option).visible is False
    expected = ProjectRef(
        project_id="g-p-project123",
        canonical_url="https://chatgpt.com/g/g-p-project123/project",
        name="Task Project",
        memory_scope=ProjectMemoryScope.PROJECT_ONLY,
    )

    async def observed(_page, *, project_id, name):
        assert project_id == expected.project_id
        assert name == expected.name
        return expected

    monkeypatch.setattr(frontend_module, "find_project_frontend", observed)

    def on_boundary() -> None:
        assert page.name == "Task Project"
        assert page.memory_scope is ProjectMemoryScope.PROJECT_ONLY
        page.boundary_entered = True
        page.events.append("boundary")

    project = await create_project_frontend(
        page,  # type: ignore[arg-type]
        name="Task Project",
        memory_scope=ProjectMemoryScope.PROJECT_ONLY,
        on_create_boundary=on_boundary,
    )

    assert project == expected
    assert page.memory_trigger_clicks == 1
    assert page.memory_option_clicks == 1
    assert page.submit_clicks == 1
    assert page.waits == [1_000, 1_000, 1_000, 1_000, 1_000, 1_000]
    assert page.events == ["trigger", "option", "boundary", "submit"]


@pytest.mark.asyncio
async def test_create_project_rejects_unproven_project_only_memory_scope(
    monkeypatch,
) -> None:
    import playwright_api.frontend as frontend_module

    page = FakePage()
    observed = ProjectRef(
        project_id="g-p-project123",
        canonical_url="https://chatgpt.com/g/g-p-project123/project",
        name="Task Project",
        memory_scope=ProjectMemoryScope.DEFAULT,
    )

    async def read_observed(_page, *, project_id, name):
        assert project_id == observed.project_id
        assert name == observed.name
        return observed

    monkeypatch.setattr(frontend_module, "find_project_frontend", read_observed)

    with pytest.raises(ConflictingIdentityError, match="memory scope"):
        await create_project_frontend(
            page,  # type: ignore[arg-type]
            name="Task Project",
            memory_scope=ProjectMemoryScope.PROJECT_ONLY,
            on_create_boundary=lambda: setattr(page, "boundary_entered", True),
        )

    assert page.submitted is True


class LookupLocator:
    def __init__(self, page: "LookupPage", kind: str, *, visible: bool = True) -> None:
        self.page = page
        self.kind = kind
        self.visible = visible

    @property
    def first(self) -> "LookupLocator":
        return self

    async def wait_for(self, *, state: str, timeout: int) -> None:
        assert state == "visible"
        assert timeout > 0
        if not self.visible:
            raise PlaywrightTimeoutError("not visible")

    async def inner_text(self) -> str:
        assert self.kind == "title"
        return self.page.read_title()

    async def evaluate(self, script: str):
        if self.kind == "details":
            assert script == "element => element.click()"
            self.page.details_open = True
            return None
        if self.kind == "settings":
            assert script == "element => element.click()"
            assert self.page.details_open is True
            self.page.settings_open = True
            return None
        if self.kind == "form":
            assert "memory_scope" in script
            return {
                "name": self.page.project_name,
                "memory_scope": self.page.memory_scope.value,
            }
        raise AssertionError(f"unexpected evaluate on {self.kind}")


class LookupPage:
    def __init__(
        self,
        *,
        row_count: int = 1,
        project_name: str = "Task Project",
        memory_scope: ProjectMemoryScope = ProjectMemoryScope.DEFAULT,
        selection_counts: list[int] | None = None,
        title_names: list[str] | None = None,
    ) -> None:
        self.project_id = "g-p-0123456789abcdef0123456789abcdef"
        self.url = "https://chatgpt.com/projects"
        self.row_count = row_count
        self.project_name = project_name
        self.memory_scope = memory_scope
        self.selection_counts = list(selection_counts or [row_count])
        self.title_names = list(title_names or [project_name])
        self.details_open = False
        self.settings_open = False

    async def goto(self, url: str, *, wait_until: str, timeout: int) -> None:
        assert wait_until == "domcontentloaded"
        assert timeout == 60_000
        self.url = url

    async def evaluate(self, script: str, name: str | None = None):
        assert 'data-page-table-selectable-row="true"' in script
        if name is None:
            return self.row_count
        assert name == "Task Project"
        count = self.selection_counts.pop(0) if len(self.selection_counts) > 1 else self.selection_counts[0]
        if count == 1:
            self.url = (
                f"https://chatgpt.com/g/{self.project_id}-task-project/project"
            )
        return {"count": count}

    async def wait_for_timeout(self, _milliseconds: int) -> None:
        return None

    def read_title(self) -> str:
        return self.title_names.pop(0) if len(self.title_names) > 1 else self.title_names[0]

    async def wait_for_url(self, pattern: str, *, timeout: int) -> None:
        assert pattern == "**/g/g-p-*/project"
        assert timeout == 60_000

    def locator(self, selector: str) -> LookupLocator:
        if selector == '[role="grid"][aria-label="Projects"]':
            return LookupLocator(self, "grid")
        if selector == 'button[name="project-title"]:visible':
            return LookupLocator(self, "title")
        if selector == 'button[aria-label="Show project details"]:visible':
            return LookupLocator(self, "details")
        if selector == 'form[aria-label="Project settings"]':
            return LookupLocator(self, "form", visible=self.settings_open)
        return LookupLocator(self, "unknown", visible=False)

    def get_by_text(self, text: str, *, exact: bool) -> LookupLocator:
        assert text == "Project settings"
        assert exact is True
        return LookupLocator(self, "settings", visible=self.details_open)


@pytest.mark.asyncio
async def test_find_project_uses_exact_owned_row_and_proves_metadata() -> None:
    page = LookupPage(memory_scope=ProjectMemoryScope.PROJECT_ONLY)

    project = await find_project_frontend(
        page,  # type: ignore[arg-type]
        name="Task Project",
    )

    assert project is not None
    assert project.project_id == page.project_id
    assert project.name == "Task Project"
    assert project.memory_scope is ProjectMemoryScope.PROJECT_ONLY
    assert project.canonical_url == f"https://chatgpt.com/g/{page.project_id}/project"


@pytest.mark.asyncio
async def test_find_project_waits_for_delayed_exact_row_hydration() -> None:
    page = LookupPage(selection_counts=[0, 1])

    project = await find_project_frontend(
        page,  # type: ignore[arg-type]
        name="Task Project",
    )

    assert project is not None
    assert project.project_id == page.project_id


@pytest.mark.asyncio
async def test_find_project_waits_for_created_title_hydration() -> None:
    page = LookupPage(title_names=["Untitled project", "Task Project"])

    project = await find_project_frontend(
        page,  # type: ignore[arg-type]
        project_id=page.project_id,
        name="Task Project",
    )

    assert project is not None
    assert project.name == "Task Project"


@pytest.mark.asyncio
async def test_find_project_fails_closed_on_duplicate_exact_rows() -> None:
    page = LookupPage(row_count=2)

    with pytest.raises(ConflictingIdentityError, match="multiple"):
        await find_project_frontend(
            page,  # type: ignore[arg-type]
            name="Task Project",
        )


@pytest.mark.asyncio
async def test_find_project_by_id_verifies_title_and_memory_scope() -> None:
    page = LookupPage()

    project = await find_project_frontend(
        page,  # type: ignore[arg-type]
        project_id=page.project_id,
    )

    assert project is not None
    assert project.project_id == page.project_id
    assert project.name == "Task Project"
    assert project.memory_scope is ProjectMemoryScope.DEFAULT
