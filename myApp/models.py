from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone
import json
import re


class Course(models.Model):
    COURSE_TYPES = [
        ('sprint', 'Sprint'),
        ('speaking', 'Speaking'),
        ('consultancy', 'Consultancy'),
        ('special', 'Special'),
    ]
    
    STATUS_CHOICES = [
        ('active', 'Active'),
        ('locked', 'Locked'),
        ('coming_soon', 'Coming Soon'),
    ]
    
    name = models.CharField(max_length=200)
    slug = models.SlugField(unique=True, max_length=200)
    course_type = models.CharField(max_length=20, choices=COURSE_TYPES, default='sprint')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='active')
    description = models.TextField()
    short_description = models.CharField(max_length=1000)
    thumbnail = models.ImageField(upload_to='course_thumbnails/', null=True, blank=True)
    coach_name = models.CharField(max_length=100, default='Sprint Coach')
    is_subscribers_only = models.BooleanField(default=False)
    is_accredible_certified = models.BooleanField(default=False)
    has_asset_templates = models.BooleanField(default=False)
    exam_unlock_days = models.IntegerField(default=120, help_text="Days after enrollment before exam unlocks")
    special_tag = models.CharField(max_length=100, blank=True, help_text="e.g., 'Black Friday 2025 Special'")
    
    # Course Availability & Access Rules
    VISIBILITY_CHOICES = [
        ('public', 'Public (visible to anyone)'),
        ('members_only', 'Members Only (visible to logged-in users)'),
        ('hidden', 'Hidden (not in catalog, direct link only)'),
        ('private', 'Private (manual assignment only)'),
    ]
    
    ENROLLMENT_METHOD_CHOICES = [
        ('open', 'Open Enrollment (free/lead magnet)'),
        ('purchase', 'Purchase Required'),
        ('invite_only', 'Invite/Assigned Only'),
        ('cohort_only', 'Cohort Only'),
        ('subscription_only', 'Subscription Only'),
    ]
    
    ACCESS_DURATION_CHOICES = [
        ('lifetime', 'Lifetime Access'),
        ('fixed_days', 'Fixed Duration (days)'),
        ('until_date', 'Access Until Date'),
        ('drip', 'Drip Schedule'),
    ]
    
    visibility = models.CharField(max_length=20, choices=VISIBILITY_CHOICES, default='public', help_text="Who can see this course exists")
    enrollment_method = models.CharField(max_length=20, choices=ENROLLMENT_METHOD_CHOICES, default='open', help_text="How students get access")
    access_duration_type = models.CharField(max_length=20, choices=ACCESS_DURATION_CHOICES, default='lifetime', help_text="Access duration rule")
    access_duration_days = models.IntegerField(null=True, blank=True, help_text="Fixed duration in days (if access_duration_type='fixed_days')")
    access_until_date = models.DateTimeField(null=True, blank=True, help_text="Access expires on this date (if access_duration_type='until_date')")
    prerequisite_courses = models.ManyToManyField('self', symmetrical=False, blank=True, related_name='unlocks_courses', help_text="Courses that must be completed first")
    required_quiz_score = models.IntegerField(null=True, blank=True, help_text="Required quiz score to unlock (0-100)")
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['-created_at']
    
    def __str__(self):
        return self.name
    
    def get_lesson_count(self):
        return self.lessons.count()
    
    def get_user_progress(self, user):
        if not user.is_authenticated:
            return 0
        completed = UserProgress.objects.filter(user=user, lesson__course=self, completed=True).count()
        total = self.lessons.count()
        if total == 0:
            return 0
        return int((completed / total) * 100)


class CourseResource(models.Model):
    """Downloadable resources for a course (SOP templates, checklists, PDFs, etc.)"""
    RESOURCE_TYPES = [
        ('template', 'Template'),
        ('checklist', 'Checklist'),
        ('pdf', 'PDF Document'),
        ('workbook', 'Workbook'),
        ('other', 'Other'),
    ]
    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name='resources')
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    resource_type = models.CharField(max_length=20, choices=RESOURCE_TYPES, default='other')
    # Either upload a file OR provide a URL (e.g. Google Drive, Dropbox)
    file = models.FileField(upload_to='course_resources/', blank=True, null=True)
    file_url = models.URLField(blank=True, help_text="External link if file is hosted elsewhere (Google Drive, etc.)")
    order = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['order', 'id']

    def __str__(self):
        return f"{self.title} - {self.course.name}"

    def get_download_url(self):
        """Return the URL to download this resource (file or external link)"""
        if self.file:
            return self.file.url
        return self.file_url


class Module(models.Model):
    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name='modules')
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    order = models.IntegerField(default=0)
    
    class Meta:
        ordering = ['order', 'id']
    
    def __str__(self):
        return f"{self.course.name} - {self.name}"


class Lesson(models.Model):
    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name='lessons')
    module = models.ForeignKey(Module, on_delete=models.SET_NULL, null=True, blank=True, related_name='lessons')
    title = models.CharField(max_length=200)
    slug = models.SlugField(max_length=200)
    description = models.TextField()
    video_url = models.URLField(blank=True)
    video_duration = models.IntegerField(default=0, help_text="Duration in minutes")
    order = models.IntegerField(default=0)
    workbook_url = models.URLField(blank=True)
    resources_url = models.URLField(blank=True)
    lesson_type = models.CharField(max_length=50, default='video', choices=[
        ('video', 'Video'),
        ('live', 'Live Session'),
        ('replay', 'Replay'),
    ])
    created_at = models.DateTimeField(auto_now_add=True)
    
    # Vimeo Integration Fields
    vimeo_url = models.URLField(blank=True, help_text="Full Vimeo URL (e.g., https://vimeo.com/123456789)")
    vimeo_id = models.CharField(max_length=50, blank=True, help_text="Vimeo video ID extracted from URL")
    vimeo_thumbnail = models.URLField(blank=True, help_text="Vimeo thumbnail URL")
    vimeo_duration_seconds = models.IntegerField(default=0, help_text="Duration in seconds from Vimeo")
    
    # Google Drive Integration Fields
    google_drive_url = models.URLField(blank=True, help_text="Google Drive video embed URL")
    google_drive_id = models.CharField(max_length=200, blank=True, help_text="Google Drive file ID")
    
    # Lesson Creation Fields
    working_title = models.CharField(max_length=200, blank=True, help_text="Rough title before AI generation")
    rough_notes = models.TextField(blank=True, help_text="Optional notes or outline for AI")
    
    # Transcription Fields
    # Note: Video files are NOT saved to the database - they are only used temporarily for transcription
    transcription = models.TextField(blank=True, help_text="Auto-generated transcription from video")
    transcription_status = models.CharField(max_length=20, default='pending', choices=[
        ('pending', 'Pending'),
        ('processing', 'Processing'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
    ])
    transcription_error = models.TextField(blank=True, help_text="Error message if transcription fails")
    
    # AI Generated Content
    ai_generation_status = models.CharField(max_length=20, default='pending', choices=[
        ('pending', 'Pending'),
        ('generated', 'Generated'),
        ('approved', 'Approved'),
    ])
    ai_clean_title = models.CharField(max_length=200, blank=True, help_text="AI-generated polished title")
    ai_short_summary = models.TextField(blank=True, help_text="AI-generated short summary for lesson list")
    ai_full_description = models.TextField(blank=True, help_text="AI-generated full description for student page")
    ai_outcomes = models.JSONField(default=list, blank=True, help_text="List of outcomes this lesson will produce")
    ai_coach_actions = models.JSONField(default=list, blank=True, help_text="Recommended AI Coach actions for this lesson")
    
    # Editor.js Content
    content = models.JSONField(default=dict, blank=True, help_text="Editor.js content blocks for lesson content")
    
    # AI Chatbot Integration Fields
    ai_chatbot_enabled = models.BooleanField(default=False, help_text="Whether AI chatbot is enabled for this lesson")
    ai_chatbot_webhook_id = models.CharField(max_length=200, blank=True, help_text="Chatbot webhook ID from training")
    ai_chatbot_trained_at = models.DateTimeField(null=True, blank=True, help_text="When transcript was sent for training")
    ai_chatbot_training_status = models.CharField(
        max_length=20, 
        default='pending', 
        choices=[
            ('pending', 'Pending'),
            ('training', 'Training'),
            ('trained', 'Trained'),
            ('failed', 'Failed'),
        ],
        help_text="Status of AI training"
    )
    ai_chatbot_training_error = models.TextField(blank=True, help_text="Error message if training fails")
    
    class Meta:
        ordering = ['order', 'id']
        unique_together = ['course', 'slug']
    
    def __str__(self):
        return f"{self.course.name} - {self.title}"
    
    def get_vimeo_embed_url(self):
        """Convert Vimeo URL to embed format"""
        if self.vimeo_id:
            return f"https://player.vimeo.com/video/{self.vimeo_id}"
        return ""
    
    def get_video_embed_url(self):
        """Convert video_url to embed format. YouTube watch/short URLs must use embed format for iframes."""
        url = (self.video_url or '').strip()
        if not url:
            return ''
        # YouTube: watch URL or youtu.be short URL -> embed
        yt_watch = re.search(r'(?:youtube\.com/watch\?v=|youtu\.be/)([a-zA-Z0-9_-]{11})', url)
        if yt_watch:
            return f"https://www.youtube.com/embed/{yt_watch.group(1)}"
        # Already embed or other URL - use as-is
        return url
    
    def get_formatted_duration(self):
        """Format duration in MM:SS format"""
        if self.vimeo_duration_seconds:
            minutes = self.vimeo_duration_seconds // 60
            seconds = self.vimeo_duration_seconds % 60
            return f"{minutes}:{seconds:02d}"
        elif self.video_duration:
            return f"{self.video_duration}:00"
        return "0:00"
    
    def get_outcomes_list(self):
        """Return outcomes as a list"""
        if isinstance(self.ai_outcomes, list):
            return self.ai_outcomes
        if isinstance(self.ai_outcomes, str):
            try:
                return json.loads(self.ai_outcomes)
            except:
                return []
        return []
    
    def get_coach_actions_list(self):
        """Return coach actions as a list"""
        if isinstance(self.ai_coach_actions, list):
            return self.ai_coach_actions
        if isinstance(self.ai_coach_actions, str):
            try:
                return json.loads(self.ai_coach_actions)
            except:
                return []
        return []


class LessonQuiz(models.Model):
    """Optional quiz that can be attached to a lesson."""
    lesson = models.OneToOneField(Lesson, on_delete=models.CASCADE, related_name='quiz')
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    is_required = models.BooleanField(default=True, help_text="If true, quiz must be passed to complete the lesson.")
    passing_score = models.IntegerField(default=70, help_text="Score percentage required to pass (0–100)")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['lesson__order', 'lesson__id']

    def __str__(self):
        return f"Quiz for {self.lesson.title}"


class LessonQuizQuestion(models.Model):
    """Multiple‑choice question for a lesson quiz."""
    OPTION_CHOICES = [
        ('A', 'Option A'),
        ('B', 'Option B'),
        ('C', 'Option C'),
        ('D', 'Option D'),
    ]

    quiz = models.ForeignKey(LessonQuiz, on_delete=models.CASCADE, related_name='questions')
    text = models.TextField()
    option_a = models.CharField(max_length=300)
    option_b = models.CharField(max_length=300)
    option_c = models.CharField(max_length=300, blank=True)
    option_d = models.CharField(max_length=300, blank=True)
    correct_option = models.CharField(max_length=1, choices=OPTION_CHOICES)
    order = models.IntegerField(default=0)

    class Meta:
        ordering = ['order', 'id']

    def __str__(self):
        return f"Q{self.order} for {self.quiz.lesson.title}"


class LessonQuizAttempt(models.Model):
    """Track a student's attempts for a lesson quiz."""
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='lesson_quiz_attempts')
    quiz = models.ForeignKey(LessonQuiz, on_delete=models.CASCADE, related_name='attempts')
    score = models.FloatField(null=True, blank=True, help_text="Score percentage (0–100)")
    passed = models.BooleanField(default=False)
    completed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-completed_at']

    def __str__(self):
        status = "Passed" if self.passed else "Failed"
        return f"{self.user.username} - {self.quiz.lesson.title} - {status}"

class UserProgress(models.Model):
    STATUS_CHOICES = [
        ('not_started', 'Not Started'),
        ('in_progress', 'In Progress'),
        ('completed', 'Completed'),
    ]
    
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='progress')
    lesson = models.ForeignKey(Lesson, on_delete=models.CASCADE, related_name='user_progress')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='not_started')
    completed = models.BooleanField(default=False)
    completed_at = models.DateTimeField(null=True, blank=True)
    progress_percentage = models.IntegerField(default=0, help_text="Overall lesson progress percentage")
    
    # Video Watch Progress Tracking
    video_watch_percentage = models.FloatField(default=0.0, help_text="Percentage of video watched (0-100)")
    last_watched_timestamp = models.FloatField(default=0.0, help_text="Last timestamp in seconds where video was watched")
    video_completion_threshold = models.FloatField(default=90.0, help_text="Required watch percentage to complete (default 90%)")
    
    last_accessed = models.DateTimeField(auto_now=True)
    started_at = models.DateTimeField(null=True, blank=True)
    
    class Meta:
        unique_together = ['user', 'lesson']
        ordering = ['-last_accessed']
    
    def __str__(self):
        return f"{self.user.username} - {self.lesson.title}"
    
    def update_status(self):
        """Automatically update status based on progress"""
        if self.video_watch_percentage >= self.video_completion_threshold:
            self.status = 'completed'
            self.completed = True
            if not self.completed_at:
                self.completed_at = timezone.now()
        elif self.video_watch_percentage > 0:
            self.status = 'in_progress'
            if not self.started_at:
                self.started_at = timezone.now()
        else:
            self.status = 'not_started'
        self.save()


class CourseEnrollment(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='enrollments')
    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name='enrollments')
    enrolled_at = models.DateTimeField(auto_now_add=True)
    payment_type = models.CharField(max_length=20, choices=[
        ('full', 'Full Payment'),
        ('installment', 'Installment'),
    ], default='full')
    
    class Meta:
        unique_together = ['user', 'course']
    
    def __str__(self):
        return f"{self.user.username} - {self.course.name}"
    
    def days_until_exam(self):
        if self.payment_type == 'full':
            return 0
        days_elapsed = (timezone.now() - self.enrolled_at).days
        return max(0, self.course.exam_unlock_days - days_elapsed)
    
    def is_exam_available(self):
        """Check if exam is available based on payment type and course completion"""
        if self.payment_type == 'full':
            # Check if all lessons are completed
            total_lessons = self.course.lessons.count()
            completed_lessons = UserProgress.objects.filter(
                user=self.user,
                lesson__course=self.course,
                completed=True
            ).count()
            return completed_lessons >= total_lessons
        else:
            return self.days_until_exam() == 0
    
    def get_certification_status(self):
        """Get current certification status"""
        try:
            cert = Certification.objects.get(user=self.user, course=self.course)
            return cert.status
        except Certification.DoesNotExist:
            # Check if eligible
            if self.is_exam_available():
                return 'eligible'
            return 'not_eligible'


class FavoriteCourse(models.Model):
    """Track user's favorite courses"""
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='favorite_courses')
    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name='favorited_by')
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        unique_together = ['user', 'course']
        ordering = ['-created_at']
    
    def __str__(self):
        return f"{self.user.username} favorited {self.course.name}"


class Exam(models.Model):
    """Final exam for a course"""
    course = models.OneToOneField(Course, on_delete=models.CASCADE, related_name='exam')
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    passing_score = models.IntegerField(default=70, help_text="Minimum score percentage to pass")
    max_attempts = models.IntegerField(default=3, help_text="Maximum number of attempts allowed (0 = unlimited)")
    time_limit_minutes = models.IntegerField(null=True, blank=True, help_text="Time limit in minutes (null = no limit)")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    def __str__(self):
        return f"{self.course.name} - {self.title}"


class ExamQuestion(models.Model):
    """Multiple-choice question for a course final exam."""
    OPTION_CHOICES = [
        ('A', 'Option A'),
        ('B', 'Option B'),
        ('C', 'Option C'),
        ('D', 'Option D'),
    ]
    exam = models.ForeignKey(Exam, on_delete=models.CASCADE, related_name='questions')
    text = models.TextField()
    option_a = models.CharField(max_length=300)
    option_b = models.CharField(max_length=300)
    option_c = models.CharField(max_length=300, blank=True)
    option_d = models.CharField(max_length=300, blank=True)
    correct_option = models.CharField(max_length=1, choices=OPTION_CHOICES)
    order = models.IntegerField(default=0)

    class Meta:
        ordering = ['order', 'id']

    def __str__(self):
        return f"Q{self.order} for {self.exam.course.name}"


class ExamAttempt(models.Model):
    """Track individual exam attempts"""
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='exam_attempts')
    exam = models.ForeignKey(Exam, on_delete=models.CASCADE, related_name='attempts')
    score = models.FloatField(null=True, blank=True, help_text="Score percentage (0-100)")
    passed = models.BooleanField(default=False)
    started_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    time_taken_seconds = models.IntegerField(null=True, blank=True)
    answers = models.JSONField(default=dict, blank=True, help_text="Student's answers")
    is_final = models.BooleanField(default=False, help_text="Whether this is the final/current attempt")
    
    class Meta:
        ordering = ['-started_at']
    
    def __str__(self):
        status = "Passed" if self.passed else "Failed"
        return f"{self.user.username} - {self.exam.course.name} - Attempt {self.attempt_number()} - {status}"
    
    def attempt_number(self):
        """Get the attempt number for this user and exam"""
        return ExamAttempt.objects.filter(
            user=self.user,
            exam=self.exam,
            started_at__lte=self.started_at
        ).count()


class Certification(models.Model):
    """Track certification status and Accredible integration"""
    STATUS_CHOICES = [
        ('not_eligible', 'Not Eligible'),
        ('eligible', 'Eligible'),
        ('passed', 'Passed - Certified'),
        ('failed', 'Failed - Retry Allowed'),
    ]
    
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='certifications')
    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name='certifications')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='not_eligible')
    
    # Accredible Integration
    accredible_certificate_id = models.CharField(max_length=200, blank=True, help_text="Accredible certificate ID")
    accredible_certificate_url = models.URLField(blank=True, help_text="Link to Accredible certificate")
    issued_at = models.DateTimeField(null=True, blank=True)
    
    # Related exam attempt that resulted in certification
    passing_exam_attempt = models.ForeignKey(
        'ExamAttempt',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='certifications'
    )
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        unique_together = ['user', 'course']
        ordering = ['-issued_at', '-created_at']
    
    def __str__(self):
        return f"{self.user.username} - {self.course.name} - {self.get_status_display()}"

# ========== ACCESS CONTROL SYSTEM ==========

class Cohort(models.Model):
    """Groups of students (e.g., 'Black Friday 2025 Buyers', 'VIP Mastermind')"""
    name = models.CharField(max_length=200, unique=True)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['-created_at']
    
    def __str__(self):
        return self.name
    
    def get_member_count(self):
        return self.members.count()
# ========== ACCESS CONTROL SYSTEM ==========

class Cohort(models.Model):
    """Groups of students (e.g., 'Black Friday 2025 Buyers', 'VIP Mastermind')"""
    name = models.CharField(max_length=200, unique=True)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['-created_at']
    
    def __str__(self):
        return self.name
    
    def get_member_count(self):
        return self.members.count()


class Bundle(models.Model):
    """Product/Bundle that grants access to multiple courses"""
    BUNDLE_TYPES = [
        ('fixed', 'Fixed Bundle (curated set)'),
        ('pick_your_own', 'Pick Your Own (choose N courses)'),
        ('tiered', 'Tiered (Bronze/Silver/Gold)'),
    ]
    
    name = models.CharField(max_length=200, unique=True)
    slug = models.SlugField(unique=True, max_length=200)
    description = models.TextField(blank=True)
    bundle_type = models.CharField(max_length=20, choices=BUNDLE_TYPES, default='fixed')
    courses = models.ManyToManyField(Course, related_name='bundles', blank=True)
    max_course_selections = models.IntegerField(null=True, blank=True, help_text="For pick-your-own bundles")
    price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['-created_at']
    
    def __str__(self):
        return self.name


class BundlePurchase(models.Model):
    """Track bundle purchases"""
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='bundle_purchases')
    bundle = models.ForeignKey(Bundle, on_delete=models.CASCADE, related_name='purchases')
    purchase_id = models.CharField(max_length=200, blank=True, help_text="External purchase/order ID")
    purchase_date = models.DateTimeField(auto_now_add=True)
    selected_courses = models.ManyToManyField(Course, blank=True, help_text="For pick-your-own bundles")
    notes = models.TextField(blank=True)
    
    class Meta:
        ordering = ['-purchase_date']
    
    def __str__(self):
        return f"{self.user.username} - {self.bundle.name}"


class CourseAccess(models.Model):
    """Explicit access record - 'Access is a thing, not a side effect'"""
    ACCESS_TYPES = [
        ('purchase', 'Purchase'),
        ('manual', 'Manual (Admin-granted)'),
        ('cohort', 'Cohort/Group'),
        ('subscription', 'Subscription/Membership'),
        ('bundle', 'Bundle Purchase'),
    ]
    
    STATUS_CHOICES = [
        ('unlocked', 'Unlocked (Active)'),
        ('locked', 'Locked'),
        ('revoked', 'Revoked'),
        ('expired', 'Expired'),
        ('pending', 'Pending'),
    ]
    
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='course_accesses')
    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name='accesses')
    access_type = models.CharField(max_length=20, choices=ACCESS_TYPES)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='unlocked')
    
    # Source tracking
    bundle_purchase = models.ForeignKey(
        BundlePurchase, 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True,
        related_name='granted_accesses'
    )
    cohort = models.ForeignKey(
        Cohort,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='granted_accesses'
    )
    purchase_id = models.CharField(max_length=200, null=True, blank=True, help_text="External purchase ID")
    
    # Dates
    granted_at = models.DateTimeField(auto_now_add=True)
    granted_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='granted_accesses',
        help_text="Admin who granted access (for manual access)"
    )
    expires_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='revoked_accesses',
        help_text="Admin who revoked access"
    )
    revocation_reason = models.CharField(max_length=200, blank=True)
    notes = models.TextField(blank=True, help_text="Support notes, audit trail")
    
    class Meta:
        ordering = ['-granted_at']
        indexes = [
            models.Index(fields=['user', 'course', 'status']),
            models.Index(fields=['status', 'expires_at']),
        ]
    
    def __str__(self):
        return f"{self.user.username} - {self.course.name} - {self.get_access_type_display()}"
    
    def is_active(self):
        """Check if access is currently active"""
        if self.status != 'unlocked':
            return False
        if self.expires_at and timezone.now() > self.expires_at:
            return False
        return True
    
    def get_source_display(self):
        """Get human-readable source of access"""
        if self.bundle_purchase:
            return f"Bundle: {self.bundle_purchase.bundle.name}"
        elif self.cohort:
            return f"Cohort: {self.cohort.name}"
        elif self.access_type == 'manual':
            return f"Manual (by {self.granted_by.username if self.granted_by else 'Admin'})"
        elif self.purchase_id:
            return f"Purchase: {self.purchase_id}"
        return self.get_access_type_display()


class CohortMember(models.Model):
    """Link users to cohorts"""
    cohort = models.ForeignKey(Cohort, on_delete=models.CASCADE, related_name='members')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='cohorts')
    joined_at = models.DateTimeField(auto_now_add=True)
    remove_access_on_leave = models.BooleanField(
        default=True,
        help_text="If True, removing from cohort revokes access. If False, access persists."
    )
    
    class Meta:
        unique_together = ['cohort', 'user']
        ordering = ['-joined_at']
    
    def __str__(self):
        return f"{self.user.username} - {self.cohort.name}"


class LearningPath(models.Model):
    """Curated learning journeys (e.g., '7-Figure Launch Path')"""
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    courses = models.ManyToManyField(Course, through='LearningPathCourse', related_name='learning_paths')
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['name']
    
    def __str__(self):
        return self.name


class LearningPathCourse(models.Model):
    """Ordered courses in a learning path"""
    learning_path = models.ForeignKey(LearningPath, on_delete=models.CASCADE)
    course = models.ForeignKey(Course, on_delete=models.CASCADE)
    order = models.IntegerField(default=0)
    is_required = models.BooleanField(default=True, help_text="Must complete to unlock next")
    
    class Meta:
        ordering = ['order']
        unique_together = ['learning_path', 'course']
    
    def __str__(self):
        return f"{self.learning_path.name} - {self.course.name} (#{self.order})"

