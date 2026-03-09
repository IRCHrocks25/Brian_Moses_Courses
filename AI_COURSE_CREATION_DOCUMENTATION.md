# AI Course Creation – Architecture & Task Handling

This document describes how the AI-powered course creation works, how background tasks are processed, and how progress is shown to the user.

---

## Overview

When an admin creates a new course with **"Generate Course Structure with AI"** enabled, the system:

1. Creates the course record immediately (no waiting)
2. Starts AI generation in a background thread
3. Redirects the user to the dashboard
4. Shows a floating progress widget (Google Drive–style) in the bottom-right corner
5. Generates 3–6 modules, 12–30 lessons, and AI content for each lesson in the background

---

## User Flow

1. Admin goes to **Dashboard → Courses → New Course**
2. Fills in: Course Name, Short Description, Full Description, Course Type, Coach Name
3. Checks **"Generate Course Structure with AI"**
4. Clicks **Create Course**
5. Immediate redirect to **Manage Courses**
6. A floating widget appears in the bottom-right showing real-time progress
7. The widget stays visible across all dashboard pages until generation finishes
8. When done: shows "Complete!" and auto-hides after ~2.5 seconds

---

## Architecture Components

### 1. Course Creation Entry Point

**File:** `myApp/dashboard_views.py`  
**View:** `dashboard_add_course`

- Handles the POST from the add-course form
- Creates the `Course` record in the database
- If AI generation is requested:
  - Stores `course_id` and `course_name` in the session
  - Writes initial progress to cache (status: `starting`)
  - Starts a Python `threading.Thread` that runs `_generate_course_ai_content`
  - Redirects to the courses list (no blocking)

### 2. Background Task: `_generate_course_ai_content`

**File:** `myApp/dashboard_views.py`

Runs in a daemon thread. Steps:

| Step | API calls | Description |
|------|-----------|-------------|
| 1 | 1 | Generate course structure (modules + lessons) via OpenAI |
| 2 | 2 per lesson | For each lesson: metadata (title, summary, outcomes, etc.) + content (Editor.js blocks) |

**Total:** For a 20-lesson course: 1 + 40 ≈ 41 OpenAI API calls. Typical duration: 2–5 minutes.

**Progress updates:** After each module and lesson, the function updates the shared cache with current status and percentage.

### 3. AI Generation Functions

| Function | Purpose | Model |
|----------|---------|-------|
| `generate_ai_course_structure()` | Produces 3–6 modules with 3–8 lessons each; returns JSON structure | gpt-4o-mini |
| `generate_ai_lesson_metadata()` | Polished title, short summary, full description, outcomes, coach actions | gpt-4o-mini |
| `generate_ai_lesson_content()` | Lesson body as Editor.js blocks (headers, paragraphs, lists, quotes) | gpt-4o-mini |
| `create_editorjs_content()` | Turns AI output into Editor.js JSON used by the lesson editor | — |

### 4. Progress Storage (Cache)

**Key:** `ai_gen_{course_id}`  
**Backend:** Django `DatabaseCache` (shared across all Gunicorn workers)

**Reason for database cache:** Production uses multiple Gunicorn workers. In-memory cache is per-worker, so the worker handling the API would often not see the background thread’s updates. Database cache is shared across workers and supports the progress widget in production.

**Cache payload:**

```json
{
  "status": "creating_content",
  "progress": 45,
  "total": 25,
  "current": "Lesson: Introduction to Financial Freedom",
  "course_name": "AI Productivity Mastery",
  "error": null
}
```

**Status values:** `starting` → `generating_structure` → `creating_content` → `completed` | `failed`

**TTL:** 15 minutes (900 seconds)

### 5. Progress API Endpoint

**URL:** `/dashboard/api/ai-generation-status/<course_id>/`  
**View:** `api_ai_generation_status`  
**Auth:** Staff only

- Reads progress from cache
- Returns JSON for the widget
- Clears session when status is `completed`, `failed`, or `unknown` so the widget stops on future page loads

### 6. Context Processor

**File:** `myApp/context_processors.py`  
**Function:** `ai_generation_context`

- Adds `ai_generating_course_id` and `ai_generating_course_name` to the template context on dashboard pages
- Used to decide whether to render the floating widget and what to show

### 7. Floating Progress Widget

**File:** `myApp/templates/dashboard/base.html`

- Rendered only when `ai_generating_course_id` is present in context
- Fixed in the bottom-right, visible on all dashboard pages
- Polls the progress API every 2.5 seconds
- Shows course name, current status, and a progress bar
- On completion: shows "Complete!", then hides after ~2.5 seconds
- On failure: shows error message; user can dismiss with the X button

---

## Configuration Requirements

### Environment

- `OPENAI_API_KEY` – required for AI generation

### Settings

```python
# settings.py – database cache (shared across workers)
CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.db.DatabaseCache',
        'LOCATION': 'django_cache',
        'OPTIONS': {'MAX_ENTRIES': 1000},
    }
}
```

### Procfile (Railway / deploy)

```
release: python manage.py migrate && python manage.py createcachetable && python manage.py collectstatic --noinput
web: gunicorn myProject.wsgi:application -c gunicorn_config.py
```

`createcachetable` creates the `django_cache` table used for the database cache.

### Gunicorn

Long-running requests and background work are handled asynchronously, but Gunicorn is configured with a longer timeout (e.g. in `gunicorn_config.py`) as a safeguard.

---

## Error Handling

- **OpenAI errors:** Caught in `_generate_course_ai_content`; status set to `failed`, error message written to cache and shown in the widget
- **JSON parse errors:** Fallback logic in metadata/content generation; basic defaults used if parsing fails
- **Cache miss (unknown status):** API clears session and returns `unknown`; widget hides so users are not stuck

---

## Logging

Background thread logs:

- `[Background] Successfully generated AI content for course "X": N modules, M lessons`
- `[Background] Error generating AI content for course "X": <error message>`

---

## Future Enhancements

- **Celery / task queue:** Celery is in `requirements.txt`; it could replace threads for more robust retries, monitoring, and scaling
- **WebSockets:** Replace polling with WebSockets for real-time updates
- **Multiple concurrent generations:** Current design assumes one active AI generation per user; could be extended to support several at once
