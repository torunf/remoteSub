// SPDX-License-Identifier: AGPL-3.0-only

package com.torunf.remotesub

import android.content.Context
import android.graphics.*
import android.text.Layout
import android.text.StaticLayout
import android.text.TextPaint
import android.util.AttributeSet
import android.view.View
import kotlin.math.max
import kotlin.math.min

/**
 * Draws the translation over the stream, like the desktop overlay: covering the subtitle area, above
 * it, or exactly where the original text is, on frosted glass or a see-through dimmed box.
 */
class SubtitleOverlayView @JvmOverloads constructor(context: Context, attrs: AttributeSet? = null): View(context, attrs)
{
	/** Where the video picture is, in this view's coordinates. */
	var videoRect: () -> RectF = { RectF(0f, 0f, width.toFloat(), height.toFloat()) }

	var userHidden = false
		set(value) { field = value; invalidate() }

	private var text: String? = null
	private var profile = Profile()
	private var textRel: RectF? = null  // the original text, as fractions of the subtitle area
	private var sourceLines = 1
	private var blurred: Bitmap? = null

	private val textPaint = TextPaint(Paint.ANTI_ALIAS_FLAG).apply {
		color = Color.rgb(246, 244, 238)
		// Montserrat Medium: a free, close match for the Chalet-style face Rockstar uses for subtitles.
		typeface = androidx.core.content.res.ResourcesCompat.getFont(context, com.metallic.chiaki.R.font.subtitle_font)
			?: Typeface.create(Typeface.DEFAULT, Typeface.BOLD)
		setShadowLayer(3f, 0f, 1.5f, Color.argb(220, 0, 0, 0))
	}
	private val boxPaint = Paint()
	private val bitmapPaint = Paint(Paint.FILTER_BITMAP_FLAG)

	fun showSubtitle(text: String, frame: Bitmap?, textRel: RectF?, sourceLines: Int, profile: Profile)
	{
		this.text = text
		this.profile = profile
		this.textRel = textRel
		this.sourceLines = max(1, sourceLines)
		blurred = if(profile.background == Background.BLUR && frame != null) blur(frame) else null
		invalidate()
	}

	fun hideSubtitle()
	{
		text = null
		blurred = null
		invalidate()
	}

	val isShowing get() = text != null && !userHidden

	override fun onDraw(canvas: Canvas)
	{
		val text = text ?: return
		val region = profile.region ?: return
		if(userHidden)
			return
		val video = videoRect()
		if(video.width() < 10 || video.height() < 10)
			return

		val area = RectF(
			video.left + region.x * video.width(), video.top + region.y * video.height(),
			video.left + (region.x + region.w) * video.width(), video.top + (region.y + region.h) * video.height())
		// Always the profile's size, in pixels of a 720p picture as on the desktop: never grown to the
		// original line (OCR boxes are too unreliable for that) nor shrunk to fit the area.
		textPaint.textSize = profile.fontSize * video.height() / 720f
		val pad = textPaint.textSize * 0.45f

		val box: RectF
		val layout: StaticLayout
		when(profile.placement)
		{
			Placement.COVER ->
			{
				layout = layout(text, area.width() - 2 * pad)
				box = RectF(area.left, area.top, area.right, max(area.bottom, area.top + layout.height + 2 * pad))
			}
			Placement.ABOVE ->
			{
				layout = layout(text, area.width() - 2 * pad)
				val h = layout.height + 2 * pad
				box = RectF(area.left, area.top - h, area.right, area.top)
			}
			Placement.MATCH ->
			{
				val rel = textRel ?: RectF(0f, 0f, 1f, 1f)
				val orig = RectF(area.left + rel.left * area.width(), area.top + rel.top * area.height(),
					area.left + rel.right * area.width(), area.top + rel.bottom * area.height())
				// As wide as the original text, widened when the translation needs more room.
				// Lines may run wider than the subtitle area: the size never shrinks to fit it.
				val oneLine = textPaint.measureText(text)
				val w = min(video.width() * 0.94f, max(orig.width() + 2 * pad, oneLine / sourceLines * 1.08f + 2 * pad))
				layout = layout(text, w - 2 * pad)
				val h = max(orig.height() + pad, layout.height + 2 * pad)
				val cx = orig.centerX().coerceIn(video.left + w / 2, video.right - w / 2)
				val cy = orig.centerY()
				box = RectF(cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)
			}
		}
		box.offset(0f, when
		{
			box.top < video.top -> video.top - box.top
			box.bottom > video.bottom -> video.bottom - box.bottom
			else -> 0f
		})

		val blurred = blurred
		if(blurred != null && profile.placement != Placement.ABOVE)
		{
			canvas.drawBitmap(blurred, null, box, bitmapPaint)
			boxPaint.color = Color.argb(110, 0, 0, 0)  // keeps the blurred original text from showing through
		}
		else
			boxPaint.color = Color.argb(profile.dimPercent * 255 / 100, 0, 0, 0)
		canvas.drawRect(box, boxPaint)

		canvas.save()
		canvas.translate(box.left + pad, box.top + (box.height() - layout.height) / 2)
		layout.draw(canvas)
		canvas.restore()
	}

	private fun layout(text: String, width: Float): StaticLayout =
		StaticLayout.Builder.obtain(text, 0, text.length, textPaint, max(1, width.toInt()))
			.setAlignment(Layout.Alignment.ALIGN_CENTER)
			.setBreakStrategy(Layout.BREAK_STRATEGY_BALANCED)
			.setLineSpacing(0f, 1.05f)
			.build()

	/** A soft copy of the picture behind the box: shrink hard, then grow back with filtering. */
	private fun blur(frame: Bitmap): Bitmap
	{
		val tiny = Bitmap.createScaledBitmap(frame, max(1, frame.width / 40), max(1, frame.height / 40), true)
		return Bitmap.createScaledBitmap(tiny, max(1, frame.width / 6), max(1, frame.height / 6), true)
	}
}
