from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth import authenticate, login, logout
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods
from django.contrib import messages
from django.conf import settings
from datetime import datetime
import json
import re
import requests
import os
import threading
from .models import (
    Course,
    Lesson,
    Module,
    UserProgress,
    CourseEnrollment,
    Exam,
    ExamAttempt,
    Certification,
    LessonQuiz,
    LessonQuizQuestion,
    LessonQuizAttempt,
)
from django.db.models import Avg, Count, Q
from django.db import models
from django.utils import timezone
from .utils.transcription import transcribe_video
from .utils.access import has_course_access


def home(request):
    """Home page view - shows landing page"""
    return render(request, 'landing.html')


def login_view(request):
    """Premium login page"""
    # Allow access to login page even when logged in if ?force=true (for testing)
    force = request.GET.get('force', '').lower() == 'true'
    if request.user.is_authenticated and not force:
        return redirect('student_dashboard')
    
    if request.method == 'POST':
        username = request.POST.get('username')
        password = request.POST.get('password')
        
        user = authenticate(request, username=username, password=password)
        if user is not None:
            login(request, user)
            next_url = request.GET.get('next', 'student_dashboard')
            return redirect(next_url)
        else:
            messages.error(request, 'Invalid username or password.')
    
    return render(request, 'login.html')


def logout_view(request):
    """Logout view"""
    logout(request)
    messages.success(request, 'You have been logged out successfully.')
    return redirect('login')


def courses(request):
    """Courses listing page"""
    course_type = request.GET.get('type', 'all')
    search_query = request.GET.get('search', '')
    
    # Optimize queries with select_related and prefetch_related
    courses = Course.objects.prefetch_related('lessons').all()
    
    if course_type != 'all':
        courses = courses.filter(course_type=course_type)
    
    if search_query:
        courses = courses.filter(name__icontains=search_query)
    
    # Get progress and favorite status for each course if user is authenticated
    courses_data = []
    in_progress_courses = []
    not_started_courses = []
    user = request.user if request.user.is_authenticated else None
    
    # Convert to list to avoid multiple queryset evaluations
    courses_list = list(courses)
    course_ids = [c.id for c in courses_list]
    
    if user:
        # Batch fetch all progress data for this user
        from .models import FavoriteCourse
        user_progress = UserProgress.objects.filter(
            user=user,
            lesson__course_id__in=course_ids
        ).values(
            'lesson__course_id',
            'completed',
            'video_watch_percentage',
            'status'
        )
        
        # Batch fetch all favorites
        favorite_course_ids = set(
            FavoriteCourse.objects.filter(user=user, course_id__in=course_ids).values_list('course_id', flat=True)
        )
        
        # Batch count lessons per course
        from django.db.models import Count
        course_lesson_counts = {
            course_id: count 
            for course_id, count in Course.objects.filter(
                id__in=course_ids
            ).annotate(lesson_count=Count('lessons')).values_list('id', 'lesson_count')
        }
        
        # Organize progress data by course
        progress_by_course = {}
        completed_by_course = {}
        for progress in user_progress:
            course_id = progress['lesson__course_id']
            if course_id not in progress_by_course:
                progress_by_course[course_id] = []
                completed_by_course[course_id] = 0
            progress_by_course[course_id].append(progress)
            if progress['completed']:
                completed_by_course[course_id] += 1
    else:
        progress_by_course = {}
        completed_by_course = {}
        favorite_course_ids = set()
        course_lesson_counts = {}
    
    for course in courses_list:
        course_info = {
            'course': course,
            'has_any_progress': False,
            'progress_percentage': 0,
            'is_favorited': False,
        }
        
        if user:
            course_id = course.id
            # Check if course has any progress (from batch data)
            has_any_progress = False
            completed_lessons = 0
            
            if course_id in progress_by_course:
                course_progresses = progress_by_course[course_id]
                has_any_progress = any(
                    p['completed'] or p['video_watch_percentage'] > 0 or 
                    p['status'] in ['in_progress', 'completed']
                    for p in course_progresses
                )
                completed_lessons = completed_by_course.get(course_id, 0)
            
            # Calculate progress percentage
            total_lessons = course_lesson_counts.get(course_id, course.lessons.count())
            progress_percentage = int((completed_lessons / total_lessons * 100)) if total_lessons > 0 else 0
            
            # Check if favorited (from batch data)
            is_favorited = course_id in favorite_course_ids
            
            course_info.update({
                'has_any_progress': has_any_progress,
                'progress_percentage': progress_percentage,
                'is_favorited': is_favorited,
            })
            
            # Separate into in-progress and not-started
            if has_any_progress:
                in_progress_courses.append(course_info)
            else:
                not_started_courses.append(course_info)
        else:
            # For non-authenticated users, put all in not_started
            not_started_courses.append(course_info)
        
        courses_data.append(course_info)
    
    return render(request, 'courses.html', {
        'courses_data': courses_data,  # Keep for backward compatibility
        'in_progress_courses': in_progress_courses,
        'not_started_courses': not_started_courses,
        'courses': courses,  # Keep for backward compatibility
        'selected_type': course_type,
        'search_query': search_query,
    })


@login_required
def course_detail(request, course_slug):
    """Course detail page - redirects to first lesson or course overview"""
    course = get_object_or_404(Course, slug=course_slug)
    first_lesson = course.lessons.first()
    
    if first_lesson:
        return lesson_detail(request, course_slug, first_lesson.slug)
    
    return render(request, 'course_detail.html', {
        'course': course,
    })


@login_required
def lesson_detail(request, course_slug, lesson_slug):
    """Lesson detail page with three-column layout"""
    from django.db.models import Prefetch
    course = get_object_or_404(
        Course.objects.prefetch_related(
            'resources',
            Prefetch('modules', queryset=Module.objects.prefetch_related('lessons').order_by('order', 'id')),
            Prefetch('lessons', queryset=Lesson.objects.select_related('module').prefetch_related('quiz', 'quiz__questions').order_by('order', 'id')),
        ),
        slug=course_slug
    )
    lesson = get_object_or_404(Lesson, course=course, slug=lesson_slug)
    
    # Get user progress with optimized queries
    enrollment = CourseEnrollment.objects.filter(
        user=request.user, 
        course=course
    ).select_related('course').first()
    
    # Batch fetch all progress data for this course (single query)
    all_progress = list(UserProgress.objects.filter(
        user=request.user,
        lesson__course=course
    ).values('lesson_id', 'completed', 'video_watch_percentage', 'last_watched_timestamp', 'status'))
    
    # Compute progress from batch data (no extra query)
    completed_lessons = [p['lesson_id'] for p in all_progress if p['completed']]
    
    # Get current lesson progress from batch data
    current_lesson_progress_data = next(
        (p for p in all_progress if p['lesson_id'] == lesson.id),
        None
    )
    
    if current_lesson_progress_data:
        video_watch_percentage = current_lesson_progress_data.get('video_watch_percentage', 0.0) or 0.0
        last_watched_timestamp = current_lesson_progress_data.get('last_watched_timestamp', 0.0) or 0.0
        lesson_status = current_lesson_progress_data.get('status', 'not_started') or 'not_started'
        # Create a mock object for template compatibility
        from types import SimpleNamespace
        current_lesson_progress = SimpleNamespace(
            video_watch_percentage=video_watch_percentage,
            last_watched_timestamp=last_watched_timestamp,
            status=lesson_status
        )
    else:
        video_watch_percentage = 0.0
        last_watched_timestamp = 0.0
        lesson_status = 'not_started'
        current_lesson_progress = None
    
    # Use prefetched lessons (no extra query)
    all_lessons = list(course.lessons.all())
    total_lessons = len(all_lessons)
    progress_percentage = int((len(completed_lessons) / total_lessons) * 100) if total_lessons > 0 else 0
    
    # Build lessons_by_module from prefetched data (avoid N+1)
    lessons_by_module = {}
    for l in all_lessons:
        mid = l.module_id or 0
        lessons_by_module.setdefault(mid, []).append(l)
    for mid in lessons_by_module:
        lessons_by_module[mid].sort(key=lambda x: (x.order, x.id))
    
    all_modules = list(course.modules.all())
    
    # Determine which lessons are accessible (using prefetched data, no N+1)
    accessible_lessons = []
    completed_set = set(completed_lessons)
    if all_lessons:
        first_lesson = all_lessons[0]
        accessible_lessons.append(first_lesson.id)
        
        for current_lesson in all_lessons[1:]:
            is_first_in_module = False
            current_module_lessons_list = lessons_by_module.get(current_lesson.module_id or 0, [])
            if current_lesson.module_id and current_module_lessons_list:
                first_lesson_in_module = current_module_lessons_list[0]
                if first_lesson_in_module.id == current_lesson.id:
                    is_first_in_module = True
                    current_module_index = next((idx for idx, m in enumerate(all_modules) if m.id == current_lesson.module_id), None)
                    if current_module_index and current_module_index > 0:
                        prev_module = all_modules[current_module_index - 1]
                        prev_module_lessons_list = lessons_by_module.get(prev_module.id, [])
                        if prev_module_lessons_list and any(lid in completed_set for lid in [l.id for l in prev_module_lessons_list]):
                            accessible_lessons.append(current_lesson.id)
                            continue
            
            if not is_first_in_module:
                if current_lesson.module_id and current_module_lessons_list:
                    current_lesson_index = next((idx for idx, l in enumerate(current_module_lessons_list) if l.id == current_lesson.id), None)
                    if current_lesson_index is not None and current_lesson_index > 0:
                        previous_lesson_in_module = current_module_lessons_list[current_lesson_index - 1]
                        if previous_lesson_in_module.id in completed_set:
                            accessible_lessons.append(current_lesson.id)
                            continue
                
                # Fallback: all previous lessons overall completed
                prev_ids = [l.id for l in all_lessons if l.order < current_lesson.order or (l.order == current_lesson.order and l.id < current_lesson.id)]
                if all(pid in completed_set for pid in prev_ids):
                    accessible_lessons.append(current_lesson.id)
        
        # Check if current lesson is locked
        lesson_locked = lesson.id not in accessible_lessons
        
        # If lesson is locked, redirect to first incomplete lesson or show message
        if lesson_locked:
            # Find first incomplete lesson
            first_incomplete = None
            for l in all_lessons:
                if l.id not in completed_lessons:
                    first_incomplete = l
                    break
            
            if first_incomplete:
                messages.warning(request, 'Please complete previous lessons before accessing this one.')
                return redirect('lesson_detail', course_slug=course_slug, lesson_slug=first_incomplete.slug)
            else:
                messages.info(request, 'All lessons completed!')
    
    # Work out next lesson (using prefetched data)
    next_lesson = None
    has_more_modules = False
    is_last_in_module = False
    
    if all_lessons and lesson.module_id:
        current_module_lessons_list = lessons_by_module.get(lesson.module_id, [])
        if current_module_lessons_list:
            last_in_module = current_module_lessons_list[-1]
            is_last_in_module = (last_in_module.id == lesson.id)
            if is_last_in_module:
                current_module_idx = next((idx for idx, m in enumerate(all_modules) if m.id == lesson.module_id), None)
                if current_module_idx is not None and current_module_idx + 1 < len(all_modules):
                    next_module = all_modules[current_module_idx + 1]
                    next_module_lessons = lessons_by_module.get(next_module.id, [])
                    if next_module_lessons:
                        next_lesson = next_module_lessons[0]
                        has_more_modules = True
            if not next_lesson:
                for idx, l in enumerate(current_module_lessons_list):
                    if l.id == lesson.id and idx + 1 < len(current_module_lessons_list):
                        next_lesson = current_module_lessons_list[idx + 1]
                        break
    
    if not next_lesson:
        for idx, l in enumerate(all_lessons):
            if l.id == lesson.id and idx + 1 < len(all_lessons):
                next_lesson = all_lessons[idx + 1]
                if lesson.module_id and next_lesson.module_id and lesson.module_id != next_lesson.module_id:
                    has_more_modules = True
                break

    # Get quiz and quiz attempts for this user (optimized)
    lesson_quiz = None
    try:
        lesson_quiz = lesson.quiz
    except:
        pass
    
    quiz_attempts = None
    latest_quiz_attempt = None
    quiz_passed = False
    if lesson_quiz:
        quiz_attempts = LessonQuizAttempt.objects.filter(
            user=request.user,
            quiz=lesson_quiz
        ).select_related('quiz', 'user').order_by('-completed_at')
        latest_quiz_attempt = quiz_attempts.first() if quiz_attempts.exists() else None
        quiz_passed = quiz_attempts.filter(passed=True).exists()

    return render(request, 'lesson.html', {
        'course': course,
        'lesson': lesson,
        'progress_percentage': progress_percentage,
        'completed_lessons': completed_lessons,
        'accessible_lessons': accessible_lessons,
        'enrollment': enrollment,
        'current_lesson_progress': current_lesson_progress,
        'video_watch_percentage': video_watch_percentage,
        'last_watched_timestamp': last_watched_timestamp,
        'lesson_status': lesson_status,
        'next_lesson': next_lesson,
        'has_more_modules': has_more_modules,
        'is_last_in_module': is_last_in_module,
        'lesson_quiz': lesson_quiz,
        'quiz_attempts': quiz_attempts,
        'latest_quiz_attempt': latest_quiz_attempt,
        'quiz_passed': quiz_passed,
    })


@login_required
def lesson_quiz_view(request, course_slug, lesson_slug):
    """Simple multiple‑choice quiz attached to a lesson (optional)."""
    course = get_object_or_404(Course, slug=course_slug)
    lesson = get_object_or_404(Lesson, course=course, slug=lesson_slug)

    # Require that a quiz exists for this lesson
    try:
        quiz = lesson.quiz
    except LessonQuiz.DoesNotExist:
        messages.info(request, 'No quiz is configured for this lesson yet.')
        return redirect('lesson_detail', course_slug=course_slug, lesson_slug=lesson_slug)

    questions = quiz.questions.all()
    result = None
    
    # Get next lesson for redirect after passing (use same logic as lesson_detail)
    all_lessons = course.lessons.order_by('order', 'id')
    next_lesson = None
    
    # Get user's completed lessons to check accessibility
    completed_lessons = list(
        UserProgress.objects.filter(
            user=request.user,
            lesson__course=course,
            completed=True
        ).values_list('lesson_id', flat=True)
    )
    
    if all_lessons.exists():
        all_modules = course.modules.all().order_by('order', 'id')
        
        # Check if current lesson has a module
        if lesson.module and all_modules.exists():
            # Get all lessons in current module, ordered
            current_module_lessons = lesson.module.lessons.filter(course=course).order_by('order', 'id')
            current_module_lessons_list = list(current_module_lessons)
            
            # Check if this is the last lesson in the current module
            is_last_in_module = False
            if current_module_lessons_list:
                last_lesson_in_module = current_module_lessons_list[-1]
                if last_lesson_in_module.id == lesson.id:
                    is_last_in_module = True
            
            if is_last_in_module:
                # Find next module's first lesson
                current_module_found = False
                for module in all_modules:
                    if current_module_found:
                        # This is the next module - get its first lesson
                        next_module_lessons = module.lessons.filter(course=course).order_by('order', 'id')
                        if next_module_lessons.exists():
                            next_lesson = next_module_lessons.first()
                            break
                    if module.id == lesson.module.id:
                        current_module_found = True
            
            # If not last in module, get next lesson in same module
            if not is_last_in_module and not next_lesson:
                for idx, l in enumerate(current_module_lessons_list):
                    if l.id == lesson.id and idx + 1 < len(current_module_lessons_list):
                        next_lesson = current_module_lessons_list[idx + 1]
                        break
        
        # Fallback: if no module or no next lesson found, use sequential navigation
        if not next_lesson:
            lessons_list = list(all_lessons)
            for idx, l in enumerate(lessons_list):
                if l.id == lesson.id and idx + 1 < len(lessons_list):
                    next_lesson = lessons_list[idx + 1]
                    break

    if request.method == 'POST':
        total = questions.count()
        correct = 0
        for q in questions:
            answer = request.POST.get(f'q_{q.id}')
            if answer and answer == q.correct_option:
                correct += 1

        score = (correct / total * 100) if total > 0 else 0
        passed = score >= quiz.passing_score

        LessonQuizAttempt.objects.create(
            user=request.user,
            quiz=quiz,
            score=score,
            passed=passed,
        )
        
        # If quiz is passed and lesson is required, auto-complete the lesson
        if passed and quiz.is_required:
            UserProgress.objects.update_or_create(
                user=request.user,
                lesson=lesson,
                defaults={
                    'completed': True,
                    'status': 'completed',
                }
            )

        result = {
            'score': round(score, 1),
            'passed': passed,
            'correct': correct,
            'total': total,
        }

    return render(request, 'lesson_quiz.html', {
        'course': course,
        'lesson': lesson,
        'quiz': quiz,
        'questions': questions,
        'result': result,
        'next_lesson': next_lesson,
    })


# ========== CREATOR DASHBOARD VIEWS ==========

@staff_member_required
def creator_dashboard(request):
    """Main creator dashboard"""
    courses = Course.objects.all()
    return render(request, 'creator/dashboard.html', {
        'courses': courses,
    })


@staff_member_required
def course_lessons(request, course_slug):
    """View all lessons for a course"""
    course = get_object_or_404(Course, slug=course_slug)
    lessons = course.lessons.all()
    modules = course.modules.all()
    
    return render(request, 'creator/course_lessons.html', {
        'course': course,
        'lessons': lessons,
        'modules': modules,
    })


@staff_member_required
def add_lesson(request, course_slug):
    """Add new lesson - 3-step flow with video upload and transcription"""
    course = get_object_or_404(Course, slug=course_slug)
    
    if request.method == 'POST':
        # Handle form submission
        vimeo_url = request.POST.get('vimeo_url', '')
        working_title = request.POST.get('working_title', '')
        rough_notes = request.POST.get('rough_notes', '')
        transcription = request.POST.get('transcription', '')
        
        # Extract Vimeo ID
        vimeo_id = extract_vimeo_id(vimeo_url) if vimeo_url else None
        
        # Create lesson draft
        lesson = Lesson.objects.create(
            course=course,
            working_title=working_title,
            rough_notes=rough_notes,
            title=working_title,  # Temporary
            slug=generate_slug(working_title),
            description='',  # Will be AI-generated
        )
        
        # Handle Vimeo URL if provided
        if vimeo_id:
            vimeo_data = fetch_vimeo_metadata(vimeo_id)
            lesson.vimeo_url = vimeo_url
            lesson.vimeo_id = vimeo_id
            lesson.vimeo_thumbnail = vimeo_data.get('thumbnail', '')
            lesson.vimeo_duration_seconds = vimeo_data.get('duration', 0)
            lesson.video_duration = vimeo_data.get('duration', 0) // 60
        
        # Handle video file upload and transcription (temporary - not saved)
        if 'video_file' in request.FILES:
            video_file = request.FILES['video_file']
            # Don't save video_file to lesson - only use for transcription
            lesson.transcription_status = 'processing'
            lesson.save()
            
            # Start transcription in background (video will be deleted after)
            def process_transcription():
                import tempfile
                temp_path = None
                try:
                    # Save to temporary file (not in media folder)
                    with tempfile.NamedTemporaryFile(delete=False, suffix='.mp4') as temp_file:
                        for chunk in video_file.chunks():
                            temp_file.write(chunk)
                        temp_path = temp_file.name
                    
                    # Transcribe from temporary file
                    result = transcribe_video(temp_path)
                    
                    # Update lesson with transcription
                    lesson.transcription_status = 'completed' if result['success'] else 'failed'
                    lesson.transcription = result.get('transcription', '')
                    lesson.transcription_error = result.get('error', '')
                    lesson.save()
                except Exception as e:
                    lesson.transcription_status = 'failed'
                    lesson.transcription_error = str(e)
                    lesson.save()
                finally:
                    # Always delete temporary video file
                    if temp_path and os.path.exists(temp_path):
                        try:
                            os.remove(temp_path)
                        except:
                            pass
            
            # Run transcription in background thread
            thread = threading.Thread(target=process_transcription)
            thread.daemon = True
            thread.start()
        elif transcription:
            # If transcription was manually edited, save it
            lesson.transcription = transcription
            lesson.transcription_status = 'completed'
        
        lesson.save()
        return redirect('generate_lesson_ai', course_slug=course_slug, lesson_id=lesson.id)
    
    return render(request, 'creator/add_lesson.html', {
        'course': course,
    })


def _save_lesson_media_and_content(lesson, request):
    """Save video URLs, workbook, resources, and content blocks from POST."""
    lesson.vimeo_url = request.POST.get('vimeo_url', lesson.vimeo_url) or ''
    lesson.google_drive_url = request.POST.get('google_drive_url', lesson.google_drive_url) or ''
    lesson.video_url = request.POST.get('video_url', lesson.video_url) or ''
    lesson.workbook_url = request.POST.get('workbook_url', lesson.workbook_url) or ''
    lesson.resources_url = request.POST.get('resources_url', lesson.resources_url) or ''
    # Vimeo: extract ID, use metadata from Verify button if provided
    if lesson.vimeo_url:
        vimeo_id = extract_vimeo_id(lesson.vimeo_url)
        if vimeo_id:
            lesson.vimeo_id = vimeo_id
    thumb = request.POST.get('vimeo_thumbnail')
    if thumb:
        lesson.vimeo_thumbnail = thumb
    dur = request.POST.get('vimeo_duration_seconds')
    if dur:
        try:
            lesson.vimeo_duration_seconds = int(dur)
        except (ValueError, TypeError):
            pass
    # Parse content blocks
    content_raw = request.POST.get('content_blocks', '')
    if content_raw:
        try:
            content_data = json.loads(content_raw)
            if isinstance(content_data, dict) and 'blocks' in content_data:
                lesson.content = content_data
            elif isinstance(content_data, list):
                lesson.content = {'blocks': content_data}
        except json.JSONDecodeError:
            pass


@staff_member_required
def generate_lesson_ai(request, course_slug, lesson_id):
    """Generate AI content for lesson"""
    course = get_object_or_404(Course, slug=course_slug)
    lesson = get_object_or_404(Lesson, id=lesson_id, course=course)
    
    if request.method == 'POST':
        action = request.POST.get('action')
        
        if action == 'generate':
            # Generate AI content
            ai_content = generate_ai_lesson_content(lesson)
            
            lesson.ai_clean_title = ai_content.get('clean_title', lesson.working_title)
            lesson.ai_short_summary = ai_content.get('short_summary', '')
            lesson.ai_full_description = ai_content.get('full_description', '')
            lesson.ai_outcomes = ai_content.get('outcomes', [])
            lesson.ai_coach_actions = ai_content.get('coach_actions', [])
            lesson.ai_generation_status = 'generated'
            lesson.save()
            
        elif action == 'approve':
            # Save video & links from form (in case not saved via Edit first)
            _save_lesson_media_and_content(lesson, request)
            # Approve and finalize lesson
            lesson.title = lesson.ai_clean_title or lesson.working_title
            lesson.description = lesson.ai_full_description
            lesson.slug = generate_slug(lesson.title)
            lesson.ai_generation_status = 'approved'
            lesson.save()
            
            return redirect('course_lessons', course_slug=course_slug)
        
        elif action == 'edit':
            # Update with manual edits
            lesson.ai_clean_title = request.POST.get('clean_title', lesson.ai_clean_title)
            lesson.ai_short_summary = request.POST.get('short_summary', lesson.ai_short_summary)
            lesson.ai_full_description = request.POST.get('full_description', lesson.ai_full_description)
            
            # Parse outcomes
            outcomes_text = request.POST.get('outcomes', '')
            if outcomes_text:
                lesson.ai_outcomes = [o.strip() for o in outcomes_text.split('\n') if o.strip()]
            
            # Parse coach actions
            coach_text = request.POST.get('coach_actions', '')
            if coach_text:
                lesson.ai_coach_actions = [a.strip() for a in coach_text.split('\n') if a.strip()]
            
            _save_lesson_media_and_content(lesson, request)
            lesson.save()
    
    # Content for JSON textarea (pass dict for json_script)
    content_data = lesson.content if (lesson.content and isinstance(lesson.content, dict)) else {'blocks': []}
    if 'blocks' not in content_data:
        content_data = {'blocks': []}
    
    return render(request, 'creator/generate_lesson_ai.html', {
        'course': course,
        'lesson': lesson,
        'content_data': content_data,
    })


@require_http_methods(["POST"])
@staff_member_required
def verify_vimeo_url(request):
    """AJAX endpoint to verify Vimeo URL and fetch metadata"""
    vimeo_url = request.POST.get('vimeo_url', '')
    vimeo_id = extract_vimeo_id(vimeo_url)
    
    if not vimeo_id:
        return JsonResponse({
            'success': False,
            'error': 'Invalid Vimeo URL format'
        })
    
    vimeo_data = fetch_vimeo_metadata(vimeo_id)
    
    if vimeo_data:
        return JsonResponse({
            'success': True,
            'vimeo_id': vimeo_id,
            'thumbnail': vimeo_data.get('thumbnail', ''),
            'duration': vimeo_data.get('duration', 0),
            'duration_formatted': format_duration(vimeo_data.get('duration', 0)),
            'title': vimeo_data.get('title', ''),
        })
    
    return JsonResponse({
        'success': False,
        'error': 'Could not fetch video metadata'
    })


@require_http_methods(["POST"])
@staff_member_required
def upload_video_transcribe(request):
    """AJAX endpoint to upload video and start transcription - video is NOT saved, only used temporarily"""
    if 'video_file' not in request.FILES:
        return JsonResponse({
            'success': False,
            'error': 'No video file provided'
        })
    
    video_file = request.FILES['video_file']
    
    # Validate file type
    if not video_file.name.lower().endswith('.mp4'):
        return JsonResponse({
            'success': False,
            'error': 'Please upload an MP4 video file'
        })
    
    # Validate file size (500MB limit)
    if video_file.size > 500 * 1024 * 1024:
        return JsonResponse({
            'success': False,
            'error': 'File size exceeds 500MB limit'
        })
    
    # Use system temp directory (not media folder) - will be deleted after transcription
    import tempfile
    temp_path = None
    
    try:
        # Save to system temporary file (outside media folder)
        with tempfile.NamedTemporaryFile(delete=False, suffix='.mp4') as temp_file:
            for chunk in video_file.chunks():
                temp_file.write(chunk)
            temp_path = temp_file.name
        
        # Transcribe from temporary file
        result = transcribe_video(temp_path)
        
        # Always delete temporary video file (we don't save videos)
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except:
                pass
        
        if result['success']:
            return JsonResponse({
                'success': True,
                'transcription': result['transcription'],
                'status': 'completed'
            })
        else:
            return JsonResponse({
                'success': False,
                'error': result.get('error', 'Transcription failed')
            })
    except Exception as e:
        # Clean up temp file on error
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except:
                pass
        return JsonResponse({
            'success': False,
            'error': str(e)
        })


@require_http_methods(["POST"])
@staff_member_required
def check_transcription_status(request, lesson_id):
    """AJAX endpoint to check transcription status"""
    lesson = get_object_or_404(Lesson, id=lesson_id)
    
    return JsonResponse({
        'status': lesson.transcription_status,
        'transcription': lesson.transcription,
        'error': lesson.transcription_error
    })


# ========== HELPER FUNCTIONS ==========

def extract_vimeo_id(url):
    """Extract Vimeo video ID from URL"""
    if not url:
        return None
    
    # Pattern: https://vimeo.com/123456789 or https://vimeo.com/123456789?param=value
    pattern = r'vimeo\.com/(\d+)'
    match = re.search(pattern, url)
    
    if match:
        return match.group(1)
    return None


def fetch_vimeo_metadata(vimeo_id):
    """Fetch metadata from Vimeo API (using oEmbed endpoint)"""
    try:
        oembed_url = f"https://vimeo.com/api/oembed.json?url=https://vimeo.com/{vimeo_id}"
        response = requests.get(oembed_url, timeout=5)
        
        if response.status_code == 200:
            data = response.json()
            return {
                'title': data.get('title', ''),
                'thumbnail': data.get('thumbnail_url', ''),
                'duration': data.get('duration', 0),
            }
    except Exception as e:
        print(f"Error fetching Vimeo metadata: {e}")
    
    return {}


def generate_ai_lesson_content(lesson):
    """Generate AI content for lesson (placeholder - connect to OpenAI later)"""
    # This is a placeholder - in production, connect to OpenAI API
    # For now, generate basic content based on working title and notes
    
    working_title = lesson.working_title or "Lesson"
    rough_notes = lesson.rough_notes or ""
    
    # Generate clean title
    clean_title = working_title.title()
    if "session" in clean_title.lower():
        clean_title = clean_title.replace("Session", "Session").replace("session", "Session")
    
    # Generate short summary
    short_summary = f"A strategic session covering key concepts from {working_title}. "
    if rough_notes:
        short_summary += "Focuses on practical implementation and actionable insights."
    else:
        short_summary += "Designed to accelerate your progress and build real assets."
    
    # Generate full description
    full_description = f"In this session, you'll dive deep into {working_title}. "
    if rough_notes:
        full_description += f"{rough_notes[:200]}... "
    full_description += "You'll learn practical strategies, implement key frameworks, and walk away with tangible outputs that move your business forward."
    
    # Generate outcomes (placeholder - should be AI-generated based on content)
    outcomes = [
        "Clear action plan for immediate implementation",
        "Key frameworks and strategies from the session",
        "Personalized insights tailored to your offer",
        "Next steps checklist for continued progress"
    ]
    
    # Generate coach actions
    coach_actions = [
        "Summarize in 5 bullets",
        "Turn this into a 3-step action plan",
        "Generate 3 email hooks from this content",
        "Give me a comprehension quiz"
    ]
    
    return {
        'clean_title': clean_title,
        'short_summary': short_summary,
        'full_description': full_description,
        'outcomes': outcomes,
        'coach_actions': coach_actions,
    }


def generate_slug(text):
    """Generate URL-friendly slug from text"""
    import unicodedata
    text = unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode('ascii')
    text = re.sub(r'[^\w\s-]', '', text.lower())
    text = re.sub(r'[-\s]+', '-', text)
    return text.strip('-')


def format_duration(seconds):
    """Format seconds as MM:SS"""
    if not seconds:
        return "0:00"
    minutes = seconds // 60
    secs = seconds % 60
    return f"{minutes}:{secs:02d}"


# ========== CHATBOT WEBHOOK ==========

@require_http_methods(["POST"])
@login_required
def update_video_progress(request, lesson_id):
    """Update video watch progress for a lesson"""
    lesson = get_object_or_404(Lesson, id=lesson_id)
    
    try:
        data = json.loads(request.body)
        watch_percentage = float(data.get('watch_percentage', 0))
        timestamp = float(data.get('timestamp', 0))
        
        # Get or create UserProgress
        user_progress, created = UserProgress.objects.get_or_create(
            user=request.user,
            lesson=lesson,
            defaults={
                'video_watch_percentage': watch_percentage,
                'last_watched_timestamp': timestamp,
                'progress_percentage': int(watch_percentage)
            }
        )
        
        # Update progress
        if not created:
            user_progress.video_watch_percentage = watch_percentage
            user_progress.last_watched_timestamp = timestamp
            user_progress.progress_percentage = int(watch_percentage)
        
        # Auto-update status based on watch progress
        user_progress.update_status()
        
        return JsonResponse({
            'success': True,
            'watch_percentage': user_progress.video_watch_percentage,
            'status': user_progress.status,
            'completed': user_progress.completed
        })
    except (json.JSONDecodeError, ValueError, KeyError) as e:
        return JsonResponse({'error': f'Invalid data: {str(e)}'}, status=400)


@require_http_methods(["POST"])
@login_required
def complete_lesson(request, lesson_id):
    """Mark a lesson as complete for the current user.
    
    If the lesson has a quiz, it must be passed before the lesson can be completed.
    """
    lesson = get_object_or_404(Lesson, id=lesson_id)
    
    # Check if lesson has a required quiz
    try:
        quiz = lesson.quiz
        if quiz.is_required:
            # Check if user has passed the quiz
            passed_attempt = LessonQuizAttempt.objects.filter(
                user=request.user,
                quiz=quiz,
                passed=True
            ).exists()
            
            if not passed_attempt:
                return JsonResponse({
                    'success': False,
                    'error': 'You must pass the lesson quiz before completing this lesson.',
                    'quiz_required': True,
                    'quiz_url': f'/courses/{lesson.course.slug}/{lesson.slug}/quiz/'
                }, status=400)
    except LessonQuiz.DoesNotExist:
        # No quiz, proceed with completion
        pass
    
    # Get or create UserProgress
    user_progress, created = UserProgress.objects.get_or_create(
        user=request.user,
        lesson=lesson
    )

    # Mark as completed
    user_progress.completed = True
    user_progress.status = 'completed'
    user_progress.completed_at = datetime.now()
    user_progress.progress_percentage = 100
    user_progress.save()
    
    return JsonResponse({
        'success': True,
        'message': 'Lesson marked as complete',
        'lesson_id': lesson_id
    })


@require_http_methods(["POST"])
@login_required
def toggle_favorite_course(request, course_id):
    """Toggle favorite status for a course"""
    from .models import FavoriteCourse, Course
    course = get_object_or_404(Course, id=course_id)
    user = request.user
    
    favorite, created = FavoriteCourse.objects.get_or_create(
        user=user,
        course=course
    )
    
    if not created:
        # Already favorited, remove it
        favorite.delete()
        is_favorited = False
    else:
        # Just favorited
        is_favorited = True
    
    return JsonResponse({
        'success': True,
        'is_favorited': is_favorited,
        'message': 'Course favorited' if is_favorited else 'Course unfavorited'
    })


@require_http_methods(["POST"])
@login_required
def chatbot_webhook(request):
    """Forward chatbot messages to the appropriate webhook based on lesson"""
    # Default webhook URL
    DEFAULT_WEBHOOK_URL = "https://kane-course-website.fly.dev/webhook/12e91cca-0e58-4769-9f11-68399ec2f970"
    
    # Lesson-specific webhook URLs
    LESSON_WEBHOOKS = {
        2: "https://kane-course-website.fly.dev/webhook/7d81ca5f-0033-4a9c-8b75-ae44005f8451",
        3: "https://kane-course-website.fly.dev/webhook/258fb5ce-b70f-48a7-b8b6-f6b0449ddbeb",
        4: "https://kane-course-website.fly.dev/webhook/19fd5879-7fc0-437d-9953-65bb70526c0b",
        5: "https://kane-course-website.fly.dev/webhook/bab1f0ef-b5bc-415f-8f73-88cc31c5c75a",
        6: "https://kane-course-website.fly.dev/webhook/6ed2483b-9c8d-4c20-85e4-432fbf033ad8",
        7: "https://kane-course-website.fly.dev/webhook/400f7a4d-3731-4ed0-90f1-35157579c7b0",
        8: "https://kane-course-website.fly.dev/webhook/0b6fee4a-bb9a-46da-831c-7d20ec7dd627",
        9: "https://kane-course-website.fly.dev/webhook/4c79ba33-2660-4816-9526-8e3513aad427",
        10: "https://kane-course-website.fly.dev/webhook/0373896c-d889-4f72-ba42-83ad6857a5e1",
        11: "https://kane-course-website.fly.dev/webhook/a571ba83-d96d-46c0-a88c-71416eda82a3",
        12: "https://kane-course-website.fly.dev/webhook/97427f57-0e89-4da3-846a-1e4453f8a58c",
    }
    
    try:
        # Get the request data
        data = json.loads(request.body)
        
        # Ensure we have a Django session and attach its ID
        if not request.session.session_key:
            request.session.save()
        data['session_id'] = request.session.session_key
        
        # Enrich payload with course/lesson code for downstream processing,
        # e.g. "virtualrockstar_session1"
        lesson_id = data.get('lesson_id')
        if lesson_id:
            try:
                lesson_obj = Lesson.objects.select_related('course').get(id=lesson_id)
                course_slug = (lesson_obj.course.slug or '').replace('-', '').replace(' ', '').lower()
                lesson_slug = (lesson_obj.slug or '').replace('-', '').replace(' ', '').lower()
                if course_slug and lesson_slug:
                    data['course_lesson_code'] = f"{course_slug}_{lesson_slug}"
            except Lesson.DoesNotExist:
                pass
        
        # Determine which webhook URL to use based on lesson_id
        webhook_url = LESSON_WEBHOOKS.get(lesson_id, DEFAULT_WEBHOOK_URL)
        
        # Forward to the webhook
        response = requests.post(
            webhook_url,
            json=data,
            headers={'Content-Type': 'application/json'},
            timeout=30
        )
        
        # Return the response from the webhook
        # Frontend treats any "error" key as a hard error, so we avoid using that
        # here and always surface the upstream payload as a normal response.
        try:
            upstream_payload = response.json()
        except ValueError:
            upstream_payload = response.text

        # Extract a clean text message for the frontend chat UI.
        message_text = None
        if isinstance(upstream_payload, list) and len(upstream_payload) > 0:
            # Handle list format like [{'output': '...'}]
            first_item = upstream_payload[0]
            if isinstance(first_item, dict):
                message_text = (
                    first_item.get('output')
                    or first_item.get('Output')
                    or first_item.get('message')
                    or first_item.get('Message')
                    or first_item.get('response')
                    or first_item.get('Response')
                    or first_item.get('text')
                    or first_item.get('Text')
                    or first_item.get('answer')
                    or first_item.get('Answer')
                )
            elif isinstance(first_item, str):
                message_text = first_item
        elif isinstance(upstream_payload, dict):
            # Many of your test webhooks wrap like: {"Response": {"output": "..."}}.
            inner = upstream_payload.get('Response', upstream_payload)
            if isinstance(inner, dict):
                message_text = (
                    inner.get('output')
                    or inner.get('Output')
                    or inner.get('message')
                    or inner.get('Message')
                    or inner.get('response')
                    or inner.get('Response')
                    or inner.get('text')
                    or inner.get('Text')
                    or inner.get('answer')
                    or inner.get('Answer')
                )
            else:
                # Try direct keys on upstream_payload
                message_text = (
                    upstream_payload.get('output')
                    or upstream_payload.get('Output')
                    or upstream_payload.get('message')
                    or upstream_payload.get('Message')
                    or upstream_payload.get('response')
                    or upstream_payload.get('Response')
                    or upstream_payload.get('text')
                    or upstream_payload.get('Text')
                    or upstream_payload.get('answer')
                    or upstream_payload.get('Answer')
                )
        if not message_text:
            message_text = str(upstream_payload)

        # Frontend expects `data.response` to be the text to display.
        return JsonResponse({'response': message_text}, status=200)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)
    except requests.RequestException as e:
        return JsonResponse({'error': str(e)}, status=500)
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


# ========== STUDENT DASHBOARD (CLIENT VIEW) ==========

@login_required
def student_dashboard(request):
    """Student dashboard - overview with access control: My Courses, Available to Unlock, Not Available"""
    user = request.user
    
    # Use access control system to organize courses
    from .utils.access import get_courses_by_visibility, has_course_access, check_course_prerequisites, batch_has_course_access
    
    courses_by_visibility = get_courses_by_visibility(user)
    my_courses = list(courses_by_visibility['my_courses'])
    available_to_unlock = list(courses_by_visibility['available_to_unlock'])
    not_available = list(courses_by_visibility['not_available'])
    
    # Also check legacy enrollments for backward compatibility
    enrollments = CourseEnrollment.objects.filter(user=user).select_related('course')
    if not enrollments.exists() and user.is_staff:
        # Auto-enroll admin/staff in all active courses
        for course in Course.objects.filter(status='active'):
            CourseEnrollment.objects.get_or_create(user=user, course=course)
        enrollments = CourseEnrollment.objects.filter(user=user).select_related('course')
    
    # Batch fetch all data for optimization
    my_course_ids = [c.id for c in my_courses]
    
    # Batch fetch enrollments
    enrollments_dict = {
        e.course_id: e 
        for e in CourseEnrollment.objects.filter(user=user, course_id__in=my_course_ids).select_related('course')
    }
    
    # Batch fetch progress data
    from django.db.models import Count
    progress_data = UserProgress.objects.filter(
        user=user,
        lesson__course_id__in=my_course_ids
    ).values('lesson__course_id').annotate(
        total_lessons=Count('lesson_id', distinct=True),
        completed_lessons=Count('lesson_id', filter=Q(completed=True), distinct=True),
        has_any_progress=Count('id', filter=Q(completed=True) | Q(video_watch_percentage__gt=0) | Q(status__in=['in_progress', 'completed'])),
        avg_watch=Avg('video_watch_percentage')
    )
    
    progress_by_course = {
        item['lesson__course_id']: item 
        for item in progress_data
    }
    
    # Batch fetch lesson counts
    course_lesson_counts = {
        course_id: count 
        for course_id, count in Course.objects.filter(id__in=my_course_ids).annotate(
            lesson_count=Count('lessons')
        ).values_list('id', 'lesson_count')
    }
    
    # Batch fetch exams
    exams_dict = {
        exam.course_id: exam 
        for exam in Exam.objects.filter(course_id__in=my_course_ids).select_related('course')
    }
    
    # Batch fetch exam attempts
    exam_attempts_by_exam = {}
    if exams_dict:
        exam_ids = list(exams_dict.keys())
        attempts = ExamAttempt.objects.filter(user=user, exam_id__in=exam_ids).select_related('exam')
        for attempt in attempts:
            exam_id = attempt.exam_id
            if exam_id not in exam_attempts_by_exam:
                exam_attempts_by_exam[exam_id] = []
            exam_attempts_by_exam[exam_id].append(attempt)
    
    # Batch fetch certifications
    certifications_dict = {
        cert.course_id: cert 
        for cert in Certification.objects.filter(user=user, course_id__in=my_course_ids).select_related('course')
    }
    
    # Batch fetch favorites
    from .models import FavoriteCourse
    favorite_course_ids = set(
        FavoriteCourse.objects.filter(user=user, course_id__in=my_course_ids).values_list('course_id', flat=True)
    )
    
    # Batch fetch course access (1 query instead of N)
    access_by_course = batch_has_course_access(user, my_course_ids)
    
    # Process My Courses (courses with access)
    my_courses_data = []
    for course in my_courses:
        has_access, access_record, _ = access_by_course.get(course.id, (False, None, "No access found"))
        if not has_access:
            continue
            
        # Get enrollment (legacy) or create access-based data
        enrollment = enrollments_dict.get(course.id)
        
        # Get progress data from batch
        course_id = course.id
        total_lessons = course_lesson_counts.get(course_id, 0)
        progress_info = progress_by_course.get(course_id, {})
        completed_lessons = progress_info.get('completed_lessons', 0)
        has_any_progress = progress_info.get('has_any_progress', 0) > 0
        avg_watch = progress_info.get('avg_watch', 0) or 0
        
        progress_percentage = int((completed_lessons / total_lessons * 100)) if total_lessons > 0 else 0
        
        # Get exam info from batch data
        exam_info = None
        exam = exams_dict.get(course_id)
        if exam:
            attempts = exam_attempts_by_exam.get(exam.id, [])
            attempts_sorted = sorted(attempts, key=lambda x: x.started_at, reverse=True)
            latest_attempt = attempts_sorted[0] if attempts_sorted else None
            exam_info = {
                'exists': True,
                'attempts_count': len(attempts),
                'max_attempts': exam.max_attempts,
                'latest_attempt': latest_attempt,
                'passed': any(a.passed for a in attempts),
                'is_available': enrollment.is_exam_available() if enrollment else False,
            }
        else:
            exam_info = {'exists': False}
        
        # Get certification status from batch data
        certification = certifications_dict.get(course_id)
        if certification:
            cert_status = certification.status
            cert_display = certification.get_status_display()
        else:
            cert_status = 'not_eligible' if progress_percentage < 100 else 'eligible'
            cert_display = 'Not Eligible' if progress_percentage < 100 else 'Eligible'
            certification = None
        
        # Check if course is favorited from batch data
        is_favorited = course_id in favorite_course_ids
        
        my_courses_data.append({
            'course': course,
            'enrollment': enrollment,
            'access_record': access_record,
            'total_lessons': total_lessons,
            'completed_lessons': completed_lessons,
            'progress_percentage': progress_percentage,
            'has_any_progress': has_any_progress,
            'avg_watch_percentage': round(avg_watch, 1),
            'exam_info': exam_info,
            'certification': certification,
            'cert_status': cert_status,
            'cert_display': cert_display,
            'is_favorited': is_favorited,
        })
    
    # Also include legacy enrollments that might not have access records yet
    existing_course_ids = {cd['course'].id for cd in my_courses_data}
    for enrollment in enrollments:
        course = enrollment.course
        # Skip if already in my_courses_data
        if course.id in existing_course_ids:
            continue
            
        # Check if course has access
        has_access, access_record, _ = has_course_access(user, course)
        if not has_access:
            # Try to create access from enrollment (migration path)
            from .utils.access import grant_course_access
            access_record = grant_course_access(
                user=user,
                course=course,
                access_type='purchase',
                notes="Migrated from legacy enrollment"
            )
        
        # Use batch data if available, otherwise calculate
        course_id = course.id
        if course_id in progress_by_course:
            progress_info = progress_by_course[course_id]
            total_lessons = course_lesson_counts.get(course_id, 0)
            completed_lessons = progress_info.get('completed_lessons', 0)
            avg_watch = progress_info.get('avg_watch', 0) or 0
        else:
            total_lessons = course_lesson_counts.get(course_id, course.lessons.count())
            completed_lessons = 0
            avg_watch = 0
        progress_percentage = int((completed_lessons / total_lessons * 100)) if total_lessons > 0 else 0
        
        # Get exam info from batch data
        exam_info = None
        exam = exams_dict.get(course_id)
        if exam:
            attempts = exam_attempts_by_exam.get(exam.id, [])
            attempts_sorted = sorted(attempts, key=lambda x: x.started_at, reverse=True)
            latest_attempt = attempts_sorted[0] if attempts_sorted else None
            exam_info = {
                'exists': True,
                'attempts_count': len(attempts),
                'max_attempts': exam.max_attempts,
                'latest_attempt': latest_attempt,
                'passed': any(a.passed for a in attempts),
                'is_available': enrollment.is_exam_available(),
            }
        else:
            exam_info = {'exists': False}
        
        # Get certification from batch data
        certification = certifications_dict.get(course_id)
        if certification:
            cert_status = certification.status
            cert_display = certification.get_status_display()
        else:
            cert_status = 'not_eligible' if progress_percentage < 100 else 'eligible'
            cert_display = 'Not Eligible' if progress_percentage < 100 else 'Eligible'
            certification = None
        
        my_courses_data.append({
            'course': course,
            'enrollment': enrollment,
            'access_record': access_record,
            'total_lessons': total_lessons,
            'completed_lessons': completed_lessons,
            'progress_percentage': progress_percentage,
            'avg_watch_percentage': round(avg_watch, 1),
            'exam_info': exam_info,
            'certification': certification,
            'cert_status': cert_status,
            'cert_display': cert_display,
        })
    
    # Batch fetch bundles for available_to_unlock (1 query instead of N)
    from .models import Bundle
    available_course_ids = [c.id for c in available_to_unlock]
    available_course_ids_set = set(available_course_ids)
    bundles_by_course = {}
    if available_course_ids:
        for bundle in Bundle.objects.filter(courses__id__in=available_course_ids, is_active=True).prefetch_related('courses'):
            for c in bundle.courses.all():
                if c.id in available_course_ids_set:
                    bundles_by_course.setdefault(c.id, []).append(bundle)
    
    # Process Available to Unlock courses
    available_courses_data = []
    for course in available_to_unlock:
        prereqs_met, missing_prereqs = check_course_prerequisites(user, course)
        available_courses_data.append({
            'course': course,
            'prereqs_met': prereqs_met,
            'missing_prereqs': missing_prereqs,
            'bundles': bundles_by_course.get(course.id, []),
        })
    
    # Process Not Available courses
    not_available_data = []
    for course in not_available:
        not_available_data.append({
            'course': course,
            'reason': course.get_visibility_display(),
        })
    
    # Get filter/sort parameters
    filter_favorites = request.GET.get('favorites', '')
    sort_by = request.GET.get('sort', 'progress')  # progress, favorites, name
    
    # Filter by favorites if requested
    if filter_favorites == 'true':
        my_courses_data = [c for c in my_courses_data if c.get('is_favorited', False)]
    
    # Sort courses
    if sort_by == 'favorites':
        # Favorites first, then by progress
        my_courses_data.sort(key=lambda x: (not x.get('is_favorited', False), -x['progress_percentage']))
    elif sort_by == 'name':
        my_courses_data.sort(key=lambda x: x['course'].name.lower())
    else:  # default: progress
        my_courses_data.sort(key=lambda x: x['progress_percentage'], reverse=True)
    
    # Overall stats
    total_courses = len(my_courses_data)
    completed_courses = sum(1 for c in my_courses_data if c['progress_percentage'] == 100)
    total_lessons_all = sum(c['total_lessons'] for c in my_courses_data)
    completed_lessons_all = sum(c['completed_lessons'] for c in my_courses_data)
    overall_progress = int((completed_lessons_all / total_lessons_all * 100)) if total_lessons_all > 0 else 0
    
    return render(request, 'student/dashboard.html', {
        'course_data': my_courses_data,  # Renamed for backward compatibility
        'my_courses': my_courses_data,
        'available_to_unlock': available_courses_data,
        'not_available': not_available_data,
        'total_courses': total_courses,
        'completed_courses': completed_courses,
        'total_lessons_all': total_lessons_all,
        'completed_lessons_all': completed_lessons_all,
        'overall_progress': overall_progress,
        'filter_favorites': filter_favorites,
        'sort_by': sort_by,
    })


@login_required
def student_course_progress(request, course_slug):
    """Detailed progress view for a specific course"""
    course = get_object_or_404(Course, slug=course_slug)
    user = request.user
    
    # Check enrollment
    enrollment = CourseEnrollment.objects.filter(user=user, course=course).select_related('course').first()
    if not enrollment:
        messages.error(request, 'You are not enrolled in this course.')
        return redirect('student_dashboard')
    
    # Get all lessons (single query)
    lessons = list(course.lessons.select_related('module').order_by('order', 'id'))
    lesson_ids = [l.id for l in lessons]
    
    # Batch fetch all UserProgress for this course (1 query instead of N)
    progress_by_lesson = {
        p.lesson_id: p
        for p in UserProgress.objects.filter(
            user=user,
            lesson_id__in=lesson_ids
        ).select_related('lesson')
    }
    
    lesson_progress = []
    for lesson in lessons:
        progress = progress_by_lesson.get(lesson.id)
        lesson_progress.append({
            'lesson': lesson,
            'progress': progress,
            'watch_percentage': progress.video_watch_percentage if progress else 0,
            'status': progress.status if progress else 'not_started',
            'completed': progress.completed if progress else False,
            'last_accessed': progress.last_accessed if progress else None,
        })
    
    # Calculate overall progress
    total_lessons = len(lessons)
    completed_lessons = sum(1 for lp in lesson_progress if lp['completed'])
    progress_percentage = int((completed_lessons / total_lessons * 100)) if total_lessons > 0 else 0
    
    # Get exam info
    exam = None
    exam_attempts = []
    try:
        exam = Exam.objects.get(course=course)
        exam_attempts = ExamAttempt.objects.filter(user=user, exam=exam).order_by('-started_at')
    except Exam.DoesNotExist:
        pass
    
    # Get certification
    try:
        certification = Certification.objects.get(user=user, course=course)
    except Certification.DoesNotExist:
        certification = None

    # Get course resources (downloadable SOP materials)
    course_resources = course.resources.all()

    return render(request, 'student/course_progress.html', {
        'course': course,
        'enrollment': enrollment,
        'lesson_progress': lesson_progress,
        'total_lessons': total_lessons,
        'completed_lessons': completed_lessons,
        'progress_percentage': progress_percentage,
        'exam': exam,
        'exam_attempts': exam_attempts,
        'certification': certification,
        'is_exam_available': enrollment.is_exam_available(),
        'course_resources': course_resources,
    })


@login_required
def student_certifications(request):
    """View all certifications"""
    user = request.user
    
    certifications = Certification.objects.filter(user=user).select_related('course').order_by('-issued_at', '-created_at')
    
    # Get eligible courses (completed but no certification yet)
    enrollments = CourseEnrollment.objects.filter(user=user).select_related('course')
    eligible_courses = []
    
    for enrollment in enrollments:
        total_lessons = enrollment.course.lessons.count()
        completed_lessons = UserProgress.objects.filter(
            user=user,
            lesson__course=enrollment.course,
            completed=True
        ).count()
        
        if completed_lessons >= total_lessons and total_lessons > 0:
            # Check if certification exists
            if not Certification.objects.filter(user=user, course=enrollment.course).exists():
                eligible_courses.append(enrollment.course)
    
    return render(request, 'student/certifications.html', {
        'certifications': certifications,
        'eligible_courses': eligible_courses,
    })


@staff_member_required
@require_http_methods(["POST"])
def train_lesson_chatbot(request, lesson_id):
    """Send transcript to training webhook and update lesson status"""
    lesson = get_object_or_404(Lesson, id=lesson_id)
    
    try:
        data = json.loads(request.body)
        transcript = data.get('transcript', '').strip()
        
        if not transcript:
            return JsonResponse({'success': False, 'error': 'Transcript is required'}, status=400)
        
        # Update lesson status
        lesson.transcription = transcript
        lesson.ai_chatbot_training_status = 'training'
        lesson.save()
        
        # Prepare payload for training webhook
        training_webhook_url = 'https://katalyst-crm2.fly.dev/webhook/425e8e67-2aa6-4c50-b67f-0162e2496b51'
        
        payload = {
            'transcript': transcript,
            'lesson_id': lesson.id,
            'lesson_title': lesson.title,
            'course_name': lesson.course.name,
            'lesson_slug': lesson.slug,
        }
        
        # Send to training webhook
        try:
            response = requests.post(
                training_webhook_url,
                json=payload,
                timeout=30,
                headers={'Content-Type': 'application/json'}
            )
            
            if response.status_code == 200:
                response_data = response.json()
                
                # Store chatbot webhook ID if returned
                chatbot_webhook_id = response_data.get('chatbot_webhook_id') or response_data.get('webhook_id') or response_data.get('id')
                
                if chatbot_webhook_id:
                    lesson.ai_chatbot_webhook_id = str(chatbot_webhook_id)
                
                lesson.ai_chatbot_training_status = 'trained'
                lesson.ai_chatbot_trained_at = timezone.now()
                lesson.ai_chatbot_enabled = True
                lesson.save()
                
                return JsonResponse({
                    'success': True,
                    'message': 'Chatbot trained successfully',
                    'chatbot_webhook_id': chatbot_webhook_id
                })
            else:
                lesson.ai_chatbot_training_status = 'failed'
                lesson.ai_chatbot_training_error = f"Webhook returned status {response.status_code}: {response.text[:500]}"
                lesson.save()
                
                return JsonResponse({
                    'success': False,
                    'error': f'Training webhook returned error: {response.status_code}'
                }, status=500)
                
        except requests.exceptions.RequestException as e:
            lesson.ai_chatbot_training_status = 'failed'
            lesson.ai_chatbot_training_error = str(e)
            lesson.save()
            
            return JsonResponse({
                'success': False,
                'error': f'Failed to connect to training webhook: {str(e)}'
            }, status=500)
            
    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'error': 'Invalid JSON'}, status=400)
    except Exception as e:
        lesson.ai_chatbot_training_status = 'failed'
        lesson.ai_chatbot_training_error = str(e)
        lesson.save()
        
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=500)


@login_required
@require_http_methods(["POST"])
def lesson_chatbot(request, lesson_id):
    """Handle chatbot interactions for a lesson"""
    lesson = get_object_or_404(Lesson, id=lesson_id)
    
    # Check if chatbot is enabled and trained
    if not lesson.ai_chatbot_enabled or lesson.ai_chatbot_training_status != 'trained':
        return JsonResponse({
            'success': False,
            'error': 'Chatbot is not available for this lesson'
        }, status=400)
    
    # Check if user has access to this lesson
    if not has_course_access(request.user, lesson.course):
        return JsonResponse({
            'success': False,
            'error': 'You do not have access to this lesson'
        }, status=403)
    
    try:
        data = json.loads(request.body)
        user_message = data.get('message', '').strip()
        
        if not user_message:
            return JsonResponse({'success': False, 'error': 'Message is required'}, status=400)
        
        # Use the chatbot webhook
        chatbot_webhook_url = 'https://katalyst-crm2.fly.dev/webhook/swi-chatbot'
        
        # Ensure we have a Django session and attach its ID
        if not request.session.session_key:
            request.session.save()
        
        payload = {
            'message': user_message,
            'lesson_id': lesson.id,
            'lesson_title': lesson.title,
            'course_name': lesson.course.name,
            'user_id': request.user.id,
            'user_email': request.user.email,
            'session_id': request.session.session_key,
            'chatbot_webhook_id': lesson.ai_chatbot_webhook_id,  # If webhook needs specific ID
        }
        
        # Send to chatbot webhook
        try:
            response = requests.post(
                chatbot_webhook_url,
                json=payload,
                timeout=30,
                headers={'Content-Type': 'application/json'}
            )
            
            if response.status_code == 200:
                # Try to parse as JSON first
                response_text = response.text
                
                # Log raw response for debugging
                import logging
                logger = logging.getLogger(__name__)
                logger.info(f"Raw webhook response for lesson {lesson.id} (first 500 chars): {response_text[:500]}")
                logger.info(f"Response headers: {dict(response.headers)}")
                
                # Print to terminal for debugging
                print("\n" + "="*80)
                print(f"AI CHATBOT RESPONSE - Lesson {lesson.id}")
                print("="*80)
                print(f"User message: {user_message}")
                print(f"Session ID: {request.session.session_key}")
                print(f"User ID: {request.user.id}")
                print(f"User Email: {request.user.email}")
                print(f"\nRaw webhook response (full):")
                print(response_text)
                print(f"\nResponse length: {len(response_text)} characters")
                
                # Check if it's HTML error page
                if response_text.strip().startswith('<!DOCTYPE') or response_text.strip().startswith('<html'):
                    return JsonResponse({
                        'success': False,
                        'error': 'Webhook returned HTML instead of JSON. Please check the webhook configuration.'
                    }, status=500)
                
                # Try to parse as JSON
                response_data = None
                try:
                    response_data = response.json()
                    logger.info(f"Parsed JSON response: {response_data}")
                    print(f"\nParsed JSON response:")
                    print(json.dumps(response_data, indent=2))
                except (ValueError, json.JSONDecodeError) as e:
                    logger.warning(
                        f"Failed to parse as JSON: {e}. Raw response (first 1000 chars): {response_text[:1000]}"
                    )
                    # Not JSON, treat as plain text / salvage malformed JSON (common when quotes are not escaped)
                    if response_text and response_text.strip():
                        cleaned_text = response_text.strip()
                        import re

                        # 1) Strong salvage: capture everything between the opening quote after Response/message/text/etc
                        # and the final quote before the closing brace. This works even if the content contains
                        # unescaped quotes (which breaks JSON).
                        key_names = ["Response", "response", "message", "Message", "text", "Text", "answer", "Answer"]
                        extracted_text = None
                        for key in key_names:
                            # Example broken JSON we see:
                            # { "Response": "Here ... \"Time Management...\" ...\nMore text" }
                            # But if quotes aren't escaped, json.loads fails; we still want the full value.
                            pattern = rf'"{re.escape(key)}"\s*:\s*"([\s\S]*)"\s*\}}'
                            m = re.search(pattern, cleaned_text)
                            if m and m.group(1) and len(m.group(1).strip()) > 0:
                                extracted_text = m.group(1)
                                break

                        # 2) Fallback: try a more conventional (escaped) match
                        if not extracted_text:
                            for key in key_names:
                                pattern = rf'"{re.escape(key)}"\s*:\s*"((?:[^"\\]|\\.)*)"'
                                m = re.search(pattern, cleaned_text, flags=re.DOTALL)
                                if m and m.group(1) and len(m.group(1).strip()) > 0:
                                    extracted_text = m.group(1)
                                    break

                        final_response = extracted_text if extracted_text else cleaned_text

                        # Unescape common sequences so the chat looks right
                        final_response = (
                            final_response.replace("\\n", "\n")
                            .replace("\\t", "\t")
                            .replace("\\r", "\r")
                            .replace('\\"', '"')
                            .replace("\\'", "'")
                        ).strip()

                        # Print final cleaned response to terminal
                        print(f"\nExtracted AI response (from non-JSON fallback):")
                        print("-"*80)
                        print(final_response)
                        print("-"*80)
                        print(f"Final response length: {len(final_response)} characters")
                        print("="*80 + "\n")

                        return JsonResponse({'success': True, 'response': final_response})

                    logger.error("Webhook returned empty response text")
                    return JsonResponse({'success': False, 'error': 'Webhook returned empty response'}, status=500)
                
                # Only process JSON response if we have response_data
                if response_data is None:
                    return JsonResponse({
                        'success': False,
                        'error': 'Webhook returned invalid response format'
                    }, status=500)
                
                # Extract AI response (adjust based on actual webhook response format)
                # Try multiple possible field names
                ai_response = None
                if isinstance(response_data, list) and len(response_data) > 0:
                    # Handle list format like [{'output': '...'}]
                    print(f"\nDetected LIST format response")
                    print(f"List length: {len(response_data)}")
                    first_item = response_data[0]
                    print(f"First item type: {type(first_item)}")
                    print(f"First item: {first_item}")
                    if isinstance(first_item, dict):
                        ai_response = (
                            first_item.get('output') or
                            first_item.get('Output') or
                            first_item.get('response') or 
                            first_item.get('Response') or 
                            first_item.get('message') or 
                            first_item.get('Message') or 
                            first_item.get('text') or 
                            first_item.get('Text') or 
                            first_item.get('answer') or 
                            first_item.get('Answer') or 
                            first_item.get('content') or
                            first_item.get('Content') or
                            None
                        )
                        print(f"Extracted from list item: {ai_response[:200] if ai_response else 'None'}...")
                        if ai_response is None:
                            print(f"WARNING: Could not extract from list item, using str(first_item)")
                            ai_response = str(first_item)
                    elif isinstance(first_item, str):
                        ai_response = first_item
                        print(f"Using string from list: {ai_response[:200]}...")
                    else:
                        print(f"WARNING: First item is not dict or string, converting to string")
                        ai_response = str(first_item)
                elif isinstance(response_data, dict):
                    print(f"\nDetected DICT format response")
                    print(f"Dict keys: {list(response_data.keys())}")
                    ai_response = (
                        response_data.get('response') or 
                        response_data.get('Response') or 
                        response_data.get('message') or 
                        response_data.get('Message') or 
                        response_data.get('text') or 
                        response_data.get('Text') or 
                        response_data.get('answer') or 
                        response_data.get('Answer') or 
                        response_data.get('content') or
                        response_data.get('Content') or
                        response_data.get('output') or
                        response_data.get('Output') or
                        None
                    )
                    
                    # If still None, try to get the first string value from the dict
                    if ai_response is None:
                        print("No standard keys found, searching for first string value...")
                        for key, value in response_data.items():
                            if isinstance(value, str) and value.strip():
                                ai_response = value
                                print(f"Found string value in key '{key}'")
                                break
                    else:
                        print(f"Extracted from dict: {ai_response[:200] if ai_response else 'None'}...")
                else:
                    # If it's not a dict or list, convert to string
                    ai_response = str(response_data)
                
                # If still None, convert entire dict to string
                if ai_response is None:
                    ai_response = str(response_data)
                
                logger.info(f"Extracted ai_response (type: {type(ai_response)}, value: {str(ai_response)[:200]})")
                
                # Clean the response - handle JSON strings and dict-like strings
                if isinstance(ai_response, str):
                    # Try to parse if it looks like JSON
                    if ai_response.strip().startswith('{') or ai_response.strip().startswith('['):
                        try:
                            parsed = json.loads(ai_response)
                            # Handle list format
                            if isinstance(parsed, list) and len(parsed) > 0:
                                first_item = parsed[0]
                                if isinstance(first_item, dict):
                                    ai_response = (
                                        first_item.get('output') or
                                        first_item.get('Output') or
                                        first_item.get('response') or
                                        first_item.get('Response') or
                                        first_item.get('message') or
                                        first_item.get('Message') or
                                        first_item.get('text') or
                                        first_item.get('Text') or
                                        first_item.get('answer') or
                                        first_item.get('Answer') or
                                        str(first_item)
                                    )
                                elif isinstance(first_item, str):
                                    ai_response = first_item
                                else:
                                    ai_response = str(first_item)
                            # Handle dict format
                            elif isinstance(parsed, dict):
                                ai_response = parsed.get('Response') or parsed.get('response') or parsed.get('message') or parsed.get('text') or parsed.get('answer') or parsed.get('output') or parsed.get('Output') or ai_response
                            else:
                                ai_response = str(parsed)
                        except (json.JSONDecodeError, TypeError):
                            # If parsing fails, try to extract quoted text
                            import re
                            # Try to extract Response field from dict-like string
                            response_match = re.search(r"['\"]Response['\"]\s*:\s*['\"]([^'\"]+)['\"]", ai_response, re.IGNORECASE)
                            if response_match:
                                ai_response = response_match.group(1)
                            else:
                                # Try to extract any quoted text that's longer than 10 chars
                                quoted_match = re.search(r"['\"]([^'\"]{10,})['\"]", ai_response)
                                if quoted_match:
                                    ai_response = quoted_match.group(1)
                
                # If response is still empty, try one more time with the full response_text
                if not ai_response or (isinstance(ai_response, str) and not ai_response.strip()):
                    logger.warning(f"Empty response extracted. Trying response_text directly.")
                    # If response_text itself is not empty, use it
                    if response_text and response_text.strip() and not response_text.strip().startswith('<!DOCTYPE') and not response_text.strip().startswith('<html'):
                        # Try to parse it as JSON one more time
                        try:
                            text_parsed = json.loads(response_text)
                            if isinstance(text_parsed, list) and len(text_parsed) > 0:
                                # Handle list format
                                first_item = text_parsed[0]
                                if isinstance(first_item, dict):
                                    ai_response = (
                                        first_item.get('output') or
                                        first_item.get('Output') or
                                        first_item.get('response') or
                                        first_item.get('Response') or
                                        first_item.get('message') or
                                        first_item.get('Message') or
                                        first_item.get('text') or
                                        first_item.get('Text') or
                                        first_item.get('answer') or
                                        first_item.get('Answer') or
                                        str(first_item)
                                    )
                                elif isinstance(first_item, str):
                                    ai_response = first_item
                                else:
                                    ai_response = str(first_item)
                            elif isinstance(text_parsed, dict):
                                ai_response = text_parsed.get('response') or text_parsed.get('Response') or text_parsed.get('message') or text_parsed.get('Message') or text_parsed.get('text') or text_parsed.get('Text') or text_parsed.get('answer') or text_parsed.get('Answer') or text_parsed.get('output') or text_parsed.get('Output') or str(text_parsed)
                            else:
                                ai_response = str(text_parsed)
                        except:
                            # If it's not JSON, use it as plain text
                            ai_response = response_text[:500]
                
                # Ensure we have a clean string response
                if not ai_response or (isinstance(ai_response, str) and (not ai_response.strip() or ai_response.strip().startswith('{'))):
                    logger.error(f"Still empty after all attempts.")
                    print(f"\n{'='*80}")
                    print(f"ERROR: Could not extract valid response after all attempts")
                    print(f"ai_response value: {ai_response}")
                    print(f"ai_response type: {type(ai_response)}")
                    print(f"{'='*80}\n")
                    return JsonResponse({
                        'success': False,
                        'error': 'The AI chatbot did not return a valid response. Please try again.'
                    }, status=500)
                
                # Ensure ai_response is a string, not a list or dict
                if isinstance(ai_response, list):
                    print(f"WARNING: ai_response is still a list, extracting text...")
                    if len(ai_response) > 0:
                        first_item = ai_response[0]
                        if isinstance(first_item, dict):
                            ai_response = (
                                first_item.get('output') or
                                first_item.get('Output') or
                                first_item.get('response') or
                                first_item.get('Response') or
                                first_item.get('message') or
                                first_item.get('Message') or
                                first_item.get('text') or
                                first_item.get('Text') or
                                first_item.get('answer') or
                                first_item.get('Answer') or
                                str(first_item)
                            )
                        elif isinstance(first_item, str):
                            ai_response = first_item
                        else:
                            ai_response = str(first_item)
                    else:
                        ai_response = str(ai_response)
                elif isinstance(ai_response, dict):
                    print(f"WARNING: ai_response is still a dict, extracting text...")
                    ai_response = (
                        ai_response.get('output') or
                        ai_response.get('Output') or
                        ai_response.get('response') or
                        ai_response.get('Response') or
                        ai_response.get('message') or
                        ai_response.get('Message') or
                        ai_response.get('text') or
                        ai_response.get('Text') or
                        ai_response.get('answer') or
                        ai_response.get('Answer') or
                        str(ai_response)
                    )
                
                # Final conversion to string
                if not isinstance(ai_response, str):
                    ai_response = str(ai_response)
                
                logger.info(f"Final ai_response: {str(ai_response)[:200]}")
                
                # Print final cleaned response to terminal
                print(f"\nExtracted AI response:")
                print("-"*80)
                print(ai_response)
                print("-"*80)
                print(f"Final response length: {len(ai_response)} characters")
                print(f"Final response type: {type(ai_response)}")
                print("="*80 + "\n")
                
                return JsonResponse({
                    'success': True,
                    'response': ai_response
                })
            else:
                print(f"\n{'='*80}")
                print(f"ERROR: Chatbot webhook returned status {response.status_code}")
                print(f"Response text: {response.text[:500]}")
                print(f"{'='*80}\n")
                return JsonResponse({
                    'success': False,
                    'error': f'Chatbot webhook returned error: {response.status_code}'
                }, status=500)
                
        except requests.exceptions.RequestException as e:
            print(f"\n{'='*80}")
            print(f"ERROR: Failed to connect to chatbot webhook")
            print(f"Error: {str(e)}")
            print(f"{'='*80}\n")
            return JsonResponse({
                'success': False,
                'error': f'Failed to connect to chatbot webhook: {str(e)}'
            }, status=500)
            
    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'error': 'Invalid JSON'}, status=400)
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=500)
