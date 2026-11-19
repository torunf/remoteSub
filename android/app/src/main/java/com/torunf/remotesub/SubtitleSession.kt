// SPDX-License-Identifier: AGPL-3.0-only

package com.torunf.remotesub

import android.graphics.Bitmap
import android.graphics.Rect
import android.graphics.RectF
import android.os.Handler
import android.os.HandlerThread
import android.os.SystemClock
import android.text.InputType
import android.util.Log
import android.view.KeyEvent
import android.view.MotionEvent
import android.view.PixelCopy
import android.view.SurfaceView
import android.view.TextureView
import android.view.View
import android.widget.EditText
import android.widget.FrameLayout
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.lifecycleScope
import androidx.lifecycle.repeatOnLifecycle
import com.google.android.material.dialog.MaterialAlertDialogBuilder
import com.google.mlkit.vision.common.InputImage
import com.google.mlkit.vision.text.TextRecognition
import com.google.mlkit.vision.text.latin.TextRecognizerOptions
import com.metallic.chiaki.R
import kotlinx.coroutines.*
import kotlinx.coroutines.tasks.await
import kotlin.coroutines.resume
import kotlin.math.max
import kotlin.math.roundToInt

/**
 * RemoteSub inside a stream screen: reads the subtitle area of the video, translates new lines and
 * draws them over the picture, the same loop as the desktop app. The video comes straight from
 * the SurfaceView the stream is decoded into, so nothing else on screen is ever read.
 *
 * The TV remote's OK (or Menu) key opens RemoteSub's menu during play; the Captions key turns
 * translation on and off. Gamepad buttons keep going to the PS5.
 */
class SubtitleSession(
	private val activity: AppCompatActivity,
	/** Where the video is drawn: a TextureView (the stream) or a SurfaceView (the demo). */
	private val videoView: View,
	private val overlay: SubtitleOverlayView,
	private val picker: RegionPickerView,
	val config: RemoteSubConfig = RemoteSubConfig(activity),
	private val allowProfiles: Boolean = true)
{
	companion object
	{
		private const val TAG = "RemoteSub"
		private const val READ_INTERVAL_MS = 140L  // between OCR reads of the area
		private const val CONFIRM_READS = 2        // a new line must read the same this often before translating
		private const val EMPTY_READS_TO_HIDE = 2  // blank reads before the original counts as gone
		private const val HOLD_MS = 4000L          // the translation stays this long after the original goes, unless a new line comes

		/** Stream screen layout helper: adds the overlay and picker on top of `root`. */
		fun attach(activity: AppCompatActivity, root: FrameLayout, videoView: View,
		           config: RemoteSubConfig = RemoteSubConfig(activity), allowProfiles: Boolean = true): SubtitleSession
		{
			val overlay = SubtitleOverlayView(activity)
			val picker = RegionPickerView(activity).apply { visibility = View.GONE }
			val params = { FrameLayout.LayoutParams(FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT) }
			root.addView(overlay, params())
			root.addView(picker, params())
			return SubtitleSession(activity, videoView, overlay, picker, config, allowProfiles)
		}
	}

	private val translator = Translator(activity, config)
	private val recognizer = TextRecognition.getClient(TextRecognizerOptions.DEFAULT_OPTIONS)
	private val captureThread = HandlerThread("remotesub-capture").apply { start() }
	private val captureHandler = Handler(captureThread.looper)

	@Volatile var active = config.translateOnStart
		private set
	@Volatile private var status = ""
	@Volatile private var lastTranslation = ""
	private var panelOpen = false

	init
	{
		val video = { videoRectIn(overlay) }
		overlay.videoRect = video
		picker.videoRect = { videoRectIn(picker) }
		activity.lifecycleScope.launch {
			launch(Dispatchers.IO) { runCatching { translator.warmUp() } }
			// A big dictionary takes seconds to parse on a TV: load it before the first subtitle.
			launch(Dispatchers.IO) { runCatching { translator.dictionaries.get(config.currentProfile().dictionaryUrl); translator.learnedCount } }
			activity.repeatOnLifecycle(Lifecycle.State.STARTED) { runLoop() }
		}
		activity.lifecycle.addObserver(object: androidx.lifecycle.DefaultLifecycleObserver {
			override fun onDestroy(owner: androidx.lifecycle.LifecycleOwner)
			{
				recognizer.close()
				translator.close()
				captureThread.quitSafely()
			}
		})
	}

	// ------------------------------------------------------------------ input

	fun dispatchKeyEvent(event: KeyEvent): Boolean
	{
		if(picker.isPicking)
			return picker.handleKey(event)
		return when(event.keyCode)
		{
			// Keys only a TV remote has; gamepad buttons are left to the game.
			KeyEvent.KEYCODE_DPAD_CENTER, KeyEvent.KEYCODE_MENU, KeyEvent.KEYCODE_ENTER, KeyEvent.KEYCODE_NUMPAD_ENTER ->
			{
				if(event.action == KeyEvent.ACTION_UP && !panelOpen)
					openPanel()
				true
			}
			KeyEvent.KEYCODE_CAPTIONS ->
			{
				if(event.action == KeyEvent.ACTION_UP)
					setActive(!active)
				true
			}
			else -> false
		}
	}

	fun onGenericMotionEvent(event: MotionEvent) = picker.isPicking && picker.handleMotion(event)

	fun setActive(on: Boolean)
	{
		active = on
		if(!on)
			overlay.hideSubtitle()
	}

	fun pickRegion()
	{
		overlay.hideSubtitle()
		picker.begin(config.currentProfile().region) { region ->
			config.updateCurrentProfile { it.copy(region = region) }
			setActive(true)
		}
	}

	// ------------------------------------------------------------------ capture → OCR → translate

	private data class OcrLine(val text: String, val box: Rect)

	private suspend fun runLoop() = withContext(Dispatchers.Default) {
		var shownSrc = ""      // source text of the translation on screen
		var candidate = ""
		var candidateReads = 0
		var firstSeen = 0L
		var emptyReads = 0
		var goneAt = 0L       // when the original line left the screen, 0 while it is there

		suspend fun hide()
		{
			if(shownSrc.isNotEmpty())
			{
				withContext(Dispatchers.Main) { overlay.hideSubtitle() }
				shownSrc = ""
			}
		}

		while(isActive)
		{
			if(!active || picker.isPicking)
			{
				status = activity.getString(R.string.rs_status_off).takeIf { !active } ?: status
				hide()
				candidate = ""; candidateReads = 0
				delay(200)
				continue
			}
			val cycleStart = SystemClock.elapsedRealtime()
			val profile = config.currentProfile()
			val region = profile.region
			if(region == null)
			{
				status = activity.getString(R.string.rs_status_no_region)
				delay(500)
				continue
			}
			val frame = grab(region)
			if(frame == null)
			{
				status = activity.getString(R.string.rs_status_no_picture)
				hide()
				delay(500)
				continue
			}

			val tOcr = SystemClock.elapsedRealtime()
			val lines = try { read(frame) } catch(e: Exception) { Log.w(TAG, "OCR failed: $e"); emptyList() }
			val ocrMs = SystemClock.elapsedRealtime() - tOcr
			status = activity.getString(R.string.rs_status_running, ocrMs.toInt()) +
				(translator.ollamaProblem?.let { " · " + activity.getString(R.string.rs_status_ollama_down) } ?: "")
			val (text, nLines, textBox) = extractText(lines)

			if(text.isEmpty() || (config.target == "tr" && looksTurkish(text)))
			{
				emptyReads++
				candidate = ""; candidateReads = 0
				if(emptyReads >= EMPTY_READS_TO_HIDE && shownSrc.isNotEmpty())
				{
					// Short lines flash by: keep the translation up a few seconds so it can be read.
					if(goneAt == 0L)
						goneAt = SystemClock.elapsedRealtime()
					else if(SystemClock.elapsedRealtime() - goneAt >= HOLD_MS)
					{
						hide()
						goneAt = 0L
					}
				}
			}
			else
			{
				emptyReads = 0
				goneAt = 0L
				when
				{
					shownSrc.isNotEmpty() && TextMatch.similar(text, shownSrc) -> { candidate = ""; candidateReads = 0 }
					candidate.isNotEmpty() && TextMatch.similar(text, candidate) -> { candidate = text; candidateReads++ }
					else -> { candidate = text; candidateReads = 1; firstSeen = tOcr }
				}

				if(candidateReads >= CONFIRM_READS)
				{
					val tTr = SystemClock.elapsedRealtime()
					val (translated, origin) = try
					{
						translator.translate(candidate, profile)
					}
					catch(e: CancellationException)
					{
						throw e
					}
					catch(e: Exception)
					{
						Log.w(TAG, "translation error: $e")
						status = activity.getString(R.string.rs_status_error, e.message ?: e.toString())
						delay(1000)
						continue
					}
					val done = SystemClock.elapsedRealtime()
					val timing = activity.getString(R.string.rs_timing, (done - firstSeen).toInt(), ocrMs.toInt(),
						(done - tTr).toInt(), activity.getString(originLabel(origin)))
					val textRel = textBox?.let {
						RectF(it.left.toFloat() / frame.width, it.top.toFloat() / frame.height,
							it.right.toFloat() / frame.width, it.bottom.toFloat() / frame.height)
					}
					withContext(Dispatchers.Main) { overlay.showSubtitle(translated, frame, textRel, nLines, profile) }
					lastTranslation = "$candidate\n→ $translated\n$timing"
					Log.i(TAG, "$timing | $candidate -> $translated")
					shownSrc = candidate; candidate = ""; candidateReads = 0
				}
			}
			delay(max(0L, READ_INTERVAL_MS - (SystemClock.elapsedRealtime() - cycleStart)))
		}
	}

	/** The subtitle area of the current video frame, or null before there is a picture. */
	private suspend fun grab(region: Region): Bitmap? = when(val v = videoView)
	{
		is TextureView -> grabTexture(v, region)
		is SurfaceView -> grabSurface(v, region)
		else -> null
	}

	private var textureFrame: Bitmap? = null

	/**
	 * Reads the TextureView's current frame (at most 1280 pixels wide, enough for OCR) and cuts
	 * out the subtitle area. getBitmap has to run on the main thread.
	 */
	private suspend fun grabTexture(view: TextureView, region: Region): Bitmap? = withContext(Dispatchers.Main) {
		if(!view.isAvailable || view.width < 100 || view.height < 100)
		{
			logCapture("no texture yet (available=${view.isAvailable}, ${view.width}x${view.height})")
			return@withContext null
		}
		val w = minOf(view.width, 1280)
		val h = (view.height.toLong() * w / view.width).toInt()
		val frame = textureFrame?.takeIf { it.width == w && it.height == h }
			?: Bitmap.createBitmap(w, h, Bitmap.Config.ARGB_8888).also { textureFrame = it }
		view.getBitmap(frame)
		val rect = Rect((region.x * w).roundToInt(), (region.y * h).roundToInt(),
			((region.x + region.w) * w).roundToInt(), ((region.y + region.h) * h).roundToInt())
		if(!rect.intersect(0, 0, w, h) || rect.width() < 8 || rect.height() < 8)
			return@withContext null
		Bitmap.createBitmap(frame, rect.left, rect.top, rect.width(), rect.height())
	}

	private suspend fun grabSurface(surfaceView: SurfaceView, region: Region): Bitmap?
	{
		val holder = surfaceView.holder
		val surface = holder.surface
		val frame = holder.surfaceFrame
		if(surface == null || !surface.isValid || frame.width() < 100 || frame.height() < 100)
		{
			logCapture("no surface (valid=${surface?.isValid}, frame=${frame.width()}x${frame.height()})")
			return null
		}
		val rect = Rect(
			(region.x * frame.width()).roundToInt(), (region.y * frame.height()).roundToInt(),
			((region.x + region.w) * frame.width()).roundToInt(), ((region.y + region.h) * frame.height()).roundToInt())
		rect.intersect(0, 0, frame.width(), frame.height())
		if(rect.width() < 8 || rect.height() < 8)
			return null
		val bitmap = Bitmap.createBitmap(rect.width(), rect.height(), Bitmap.Config.ARGB_8888)
		return suspendCancellableCoroutine { cont ->
			try
			{
				PixelCopy.request(surfaceView, rect, bitmap, { result ->
					if(result != PixelCopy.SUCCESS)
						logCapture("PixelCopy failed: $result (rect=$rect)")
					if(cont.isActive)
						cont.resume(if(result == PixelCopy.SUCCESS) bitmap else null)
				}, captureHandler)
			}
			catch(e: IllegalArgumentException)  // the surface went away in between
			{
				if(cont.isActive)
					cont.resume(null)
			}
		}
	}

	private var lastCaptureLog = 0L

	/** Capture problems, at most every few seconds so the log stays readable. */
	private fun logCapture(message: String)
	{
		val now = SystemClock.elapsedRealtime()
		if(now - lastCaptureLog > 5000)
		{
			lastCaptureLog = now
			Log.w(TAG, "capture: $message")
		}
	}

	private suspend fun read(bitmap: Bitmap): List<OcrLine> =
		recognizer.process(InputImage.fromBitmap(bitmap, 0)).await()
			.textBlocks.flatMap { it.lines }
			.mapNotNull { line -> line.boundingBox?.let { OcrLine(line.text, it) } }
			.sortedBy { it.box.top }

	/** Subtitle lines joined into one sentence, how many there were, and the box around them. */
	private fun extractText(lines: List<OcrLine>): Triple<String, Int, Rect?>
	{
		val kept = lines.map { OcrLine(it.text.split(Regex("\\s+")).filter { w -> w.isNotEmpty() }.joinToString(" "), it.box) }
			.filter { l -> l.text.count { it.isLetter() } >= 2 }
		if(kept.isEmpty())
			return Triple("", 0, null)
		val union = Rect(kept[0].box)
		kept.forEach { union.union(it.box) }
		return Triple(kept.joinToString(" ") { it.text }, kept.size, union)
	}

	private fun looksTurkish(text: String) = text.any { it in "ğĞışŞİ" }

	private fun originLabel(origin: Origin) = when(origin)
	{
		Origin.DICTIONARY -> R.string.rs_origin_dictionary
		Origin.CACHE -> R.string.rs_origin_cache
		Origin.OLLAMA -> R.string.rs_origin_ollama
		Origin.ON_DEVICE -> R.string.rs_origin_on_device
	}

	/** Where the video is drawn, in `view`'s coordinates. */
	private fun videoRectIn(view: View): RectF
	{
		val a = IntArray(2)
		val b = IntArray(2)
		videoView.getLocationInWindow(a)
		view.getLocationInWindow(b)
		val left = (a[0] - b[0]).toFloat()
		val top = (a[1] - b[1]).toFloat()
		return RectF(left, top, left + videoView.width, top + videoView.height)
	}

	// ------------------------------------------------------------------ menu

	fun openPanel()
	{
		val profile = config.currentProfile()
		val a = activity
		val items = mutableListOf<Pair<String, () -> Unit>>()
		items += a.getString(if(active) R.string.rs_panel_translation_on else R.string.rs_panel_translation_off) to {
			setActive(!active); reopen()
		}
		items += a.getString(R.string.rs_panel_region) to { pickRegion() }
		if(allowProfiles)
			items += a.getString(R.string.rs_panel_profile, config.currentProfileName) to { chooseProfile() }
		items += a.getString(R.string.rs_panel_placement, a.getString(placementLabel(profile.placement))) to { choosePlacement() }
		items += a.getString(R.string.rs_panel_background, a.getString(backgroundLabel(profile.background))) to { chooseBackground() }
		items += a.getString(R.string.rs_panel_dim, profile.dimPercent) to { chooseDim() }
		items += a.getString(R.string.rs_panel_size, profile.fontSize) to { chooseSize() }
		items += a.getString(R.string.rs_panel_dictionary,
			profile.dictionaryUrl.ifEmpty { a.getString(R.string.rs_panel_dictionary_none) }) to { editDictionary() }
		items += a.getString(if(overlay.userHidden) R.string.rs_panel_show else R.string.rs_panel_hide) to {
			overlay.userHidden = !overlay.userHidden
		}
		items += a.getString(R.string.rs_panel_close) to { }

		val message = buildString {
			append(status)
			if(lastTranslation.isNotEmpty())
				append("\n\n").append(a.getString(R.string.rs_last)).append(":\n").append(lastTranslation)
		}
		val titleView = android.widget.TextView(a).apply {
			text = a.getString(R.string.rs_panel_title) + "\n" + message
			setPadding(48, 36, 48, 12)
			textSize = 14f
			setTextColor(android.graphics.Color.argb(222, 255, 255, 255))
		}
		panelOpen = true
		MaterialAlertDialogBuilder(a)
			.setCustomTitle(titleView)
			.setItems(items.map { it.first }.toTypedArray()) { _, which -> items[which].second() }
			.setOnDismissListener { panelOpen = false }
			.show()
	}

	private fun reopen() = activity.window.decorView.post { openPanel() }

	private fun choose(title: Int, labels: List<String>, checked: Int, onChosen: (Int) -> Unit)
	{
		MaterialAlertDialogBuilder(activity)
			.setTitle(title)
			.setSingleChoiceItems(labels.toTypedArray(), checked) { dialog, which ->
				onChosen(which)
				dialog.dismiss()
				reopen()
			}
			.setOnCancelListener { reopen() }
			.show()
	}

	private fun choosePlacement()
	{
		val values = Placement.values()
		choose(R.string.rs_placement_title, values.map { activity.getString(placementLabel(it)) },
			values.indexOf(config.currentProfile().placement)) { i -> config.updateCurrentProfile { it.copy(placement = values[i]) }; overlay.hideSubtitle() }
	}

	private fun chooseBackground()
	{
		val values = Background.values()
		choose(R.string.rs_background_title, values.map { activity.getString(backgroundLabel(it)) },
			values.indexOf(config.currentProfile().background)) { i -> config.updateCurrentProfile { it.copy(background = values[i]) }; overlay.hideSubtitle() }
	}

	private fun chooseDim()
	{
		val values = (20..95 step 5).toList()
		val current = config.currentProfile().dimPercent
		choose(R.string.rs_dim_title, values.map { "$it%" }, values.indexOfFirst { it >= current }.coerceAtLeast(0)) { i ->
			config.updateCurrentProfile { it.copy(dimPercent = values[i]) }; overlay.hideSubtitle()
		}
	}

	private fun chooseSize()
	{
		val values = (12..48 step 2).toList()
		val current = config.currentProfile().fontSize
		choose(R.string.rs_size_title, values.map { it.toString() }, values.indexOfFirst { it >= current }.coerceAtLeast(0)) { i ->
			config.updateCurrentProfile { it.copy(fontSize = values[i]) }; overlay.hideSubtitle()
		}
	}

	private fun chooseProfile()
	{
		val names = config.profiles().keys.toMutableList()
		if(config.currentProfileName !in names)
			names.add(0, config.currentProfileName)
		val labels = names + activity.getString(R.string.rs_profile_new) + activity.getString(R.string.rs_profile_delete)
		choose(R.string.rs_profile_title, labels, names.indexOf(config.currentProfileName)) { i ->
			when
			{
				i < names.size -> config.selectProfile(names[i])
				i == names.size -> askText(R.string.rs_profile_new, R.string.rs_profile_name_hint, "") { name ->
					if(name.isNotBlank()) config.selectProfile(name.trim())
				}
				else -> config.deleteCurrentProfile()
			}
			overlay.hideSubtitle()
		}
	}

	private fun editDictionary()
	{
		askText(R.string.rs_panel_dictionary_title, R.string.rs_dictionary_url_hint, config.currentProfile().dictionaryUrl,
			R.string.rs_dictionary_message) { url -> config.updateCurrentProfile { it.copy(dictionaryUrl = url.trim()) } }
	}

	private fun askText(title: Int, hint: Int, value: String, message: Int? = null, onDone: (String) -> Unit)
	{
		val edit = EditText(activity).apply {
			setText(value)
			setHint(hint)
			inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_URI
			setSingleLine()
		}
		val container = FrameLayout(activity).apply {
			setPadding(56, 8, 56, 0)
			addView(edit)
		}
		MaterialAlertDialogBuilder(activity)
			.setTitle(title)
			.also { if(message != null) it.setMessage(message) }
			.setView(container)
			.setPositiveButton(android.R.string.ok) { _, _ -> onDone(edit.text.toString()); reopen() }
			.setNegativeButton(android.R.string.cancel) { _, _ -> reopen() }
			.show()
	}

	private fun placementLabel(p: Placement) = when(p)
	{
		Placement.COVER -> R.string.rs_placement_cover
		Placement.ABOVE -> R.string.rs_placement_above
		Placement.MATCH -> R.string.rs_placement_match
	}

	private fun backgroundLabel(b: Background) = when(b)
	{
		Background.BLUR -> R.string.rs_background_blur
		Background.DIM -> R.string.rs_background_dim
	}
}
