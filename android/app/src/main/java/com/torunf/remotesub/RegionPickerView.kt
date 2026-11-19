// SPDX-License-Identifier: AGPL-3.0-only

package com.torunf.remotesub

import android.content.Context
import android.graphics.*
import android.util.AttributeSet
import android.view.InputDevice
import android.view.KeyEvent
import android.view.MotionEvent
import android.view.View
import androidx.core.view.isVisible
import com.metallic.chiaki.R
import kotlin.math.abs

/**
 * Choosing the subtitle area with a TV remote or a gamepad: the arrows move the box, OK switches
 * between moving and resizing, Back saves.
 */
class RegionPickerView @JvmOverloads constructor(context: Context, attrs: AttributeSet? = null): View(context, attrs)
{
	var videoRect: () -> RectF = { RectF(0f, 0f, width.toFloat(), height.toFloat()) }

	private var region = Region.DEFAULT
	private var resizing = false
	private var onDone: ((Region) -> Unit)? = null
	private var hatX = 0f
	private var hatY = 0f

	private val shade = Paint().apply { color = Color.argb(140, 0, 0, 0) }
	private val border = Paint(Paint.ANTI_ALIAS_FLAG).apply { style = Paint.Style.STROKE }
	private val hintPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
		color = Color.WHITE
		textAlign = Paint.Align.CENTER
		setShadowLayer(4f, 0f, 1f, Color.BLACK)
	}
	private val hintBox = Paint().apply { color = Color.argb(170, 0, 0, 0) }

	val isPicking get() = isVisible

	fun begin(initial: Region?, onDone: (Region) -> Unit)
	{
		region = initial ?: Region.DEFAULT
		resizing = false
		this.onDone = onDone
		isVisible = true
		invalidate()
	}

	private fun finish()
	{
		isVisible = false
		onDone?.invoke(region)
		onDone = null
	}

	/** Every key goes to the picker while it is open, so nothing reaches the game. */
	fun handleKey(event: KeyEvent): Boolean
	{
		if(event.action != KeyEvent.ACTION_DOWN)
			return true
		// Held keys speed up after a moment.
		val step = if(event.repeatCount > 6) 0.015f else 0.004f
		when(event.keyCode)
		{
			KeyEvent.KEYCODE_DPAD_LEFT -> nudge(-step, 0f)
			KeyEvent.KEYCODE_DPAD_RIGHT -> nudge(step, 0f)
			KeyEvent.KEYCODE_DPAD_UP -> nudge(0f, -step)
			KeyEvent.KEYCODE_DPAD_DOWN -> nudge(0f, step)
			KeyEvent.KEYCODE_DPAD_CENTER, KeyEvent.KEYCODE_ENTER, KeyEvent.KEYCODE_NUMPAD_ENTER,
			KeyEvent.KEYCODE_BUTTON_A, KeyEvent.KEYCODE_BUTTON_X ->
			{
				if(event.repeatCount == 0)
				{
					resizing = !resizing
					invalidate()
				}
			}
			KeyEvent.KEYCODE_BACK, KeyEvent.KEYCODE_BUTTON_B, KeyEvent.KEYCODE_BUTTON_START,
			KeyEvent.KEYCODE_ESCAPE, KeyEvent.KEYCODE_MENU -> finish()
		}
		return true
	}

	/** The gamepad's d-pad arrives as hat axis motion rather than keys. */
	fun handleMotion(event: MotionEvent): Boolean
	{
		if(event.source and InputDevice.SOURCE_JOYSTICK != InputDevice.SOURCE_JOYSTICK)
			return true
		val x = event.getAxisValue(MotionEvent.AXIS_HAT_X)
		val y = event.getAxisValue(MotionEvent.AXIS_HAT_Y)
		if(abs(x) > 0.5f && abs(hatX) <= 0.5f) nudge(0.01f * Math.signum(x), 0f)
		if(abs(y) > 0.5f && abs(hatY) <= 0.5f) nudge(0f, 0.01f * Math.signum(y))
		hatX = x
		hatY = y
		return true
	}

	private fun nudge(dx: Float, dy: Float)
	{
		region = if(resizing)
		{
			val w = (region.w + dx).coerceIn(0.05f, 1f - region.x)
			val h = (region.h + dy).coerceIn(0.03f, 1f - region.y)
			region.copy(w = w, h = h)
		}
		else
			region.copy(x = (region.x + dx).coerceIn(0f, 1f - region.w), y = (region.y + dy).coerceIn(0f, 1f - region.h))
		invalidate()
	}

	override fun onDraw(canvas: Canvas)
	{
		val video = videoRect()
		val r = RectF(video.left + region.x * video.width(), video.top + region.y * video.height(),
			video.left + (region.x + region.w) * video.width(), video.top + (region.y + region.h) * video.height())

		// Darken everything but the chosen area.
		canvas.drawRect(video.left, video.top, video.right, r.top, shade)
		canvas.drawRect(video.left, r.bottom, video.right, video.bottom, shade)
		canvas.drawRect(video.left, r.top, r.left, r.bottom, shade)
		canvas.drawRect(r.right, r.top, video.right, r.bottom, shade)

		val density = resources.displayMetrics.density
		border.strokeWidth = 3 * density
		border.color = if(resizing) Color.rgb(255, 193, 7) else Color.WHITE
		canvas.drawRect(r, border)
		if(resizing)
		{
			val s = 10 * density
			border.style = Paint.Style.FILL
			canvas.drawRect(r.right - s, r.bottom - s, r.right + s, r.bottom + s, border)
			border.style = Paint.Style.STROKE
		}

		hintPaint.textSize = 18 * density
		val hint = context.getString(if(resizing) R.string.rs_picker_hint_resize else R.string.rs_picker_hint_move)
		val tw = hintPaint.measureText(hint)
		// Below the top edge, clear of anything a game or the demo shows in the corner.
		val ty = video.top + video.height() * 0.2f
		canvas.drawRoundRect(video.centerX() - tw / 2 - 16 * density, ty - 28 * density,
			video.centerX() + tw / 2 + 16 * density, ty + 12 * density, 8 * density, 8 * density, hintBox)
		canvas.drawText(hint, video.centerX(), ty, hintPaint)
	}
}
