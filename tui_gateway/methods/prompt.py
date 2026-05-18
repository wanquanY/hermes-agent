# ruff: noqa: F401,F403,F405,F821,ARG001
from __future__ import annotations

import json

from tui_gateway.methods._shared import bind_server_globals
from tui_gateway.services.voice import voice_tts_enabled

_server = bind_server_globals(globals())


# ── Methods: prompt ──────────────────────────────────────────────────


@method("prompt.submit")
def _(rid, params: dict) -> dict:
    sid, text = params.get("session_id", ""), params.get("text", "")
    requested_model = str(params.get("model") or "").strip()
    model_descriptor = _normalize_model_descriptor(params.get("model_descriptor") or params.get("modelDescriptor"))
    has_model_descriptor = bool(params.get("model_descriptor") or params.get("modelDescriptor"))
    run_id = str(params.get("client_run_id") or params.get("run_id") or uuid.uuid4().hex).strip()
    turn_id = str(params.get("turn_id") or uuid.uuid4().hex).strip()
    client_message_id = str(params.get("client_message_id") or "").strip()
    raw_doxie_context = params.get("doxie_product_context") or params.get("doxieProductContext") or ""
    doxie_product_context = (
        json.dumps(raw_doxie_context, ensure_ascii=False)
        if isinstance(raw_doxie_context, (dict, list))
        else str(raw_doxie_context or "").strip()
    )
    submitted_images = _submitted_image_paths(params)
    submitted_attachments = _submitted_attachments(params)
    session, err = _sess_nowait(params, rid)
    if err:
        return err
    with session["history_lock"]:
        if session.get("running"):
            return _err(rid, 4009, "session busy")
        session["running"] = True
        session["active_run_id"] = run_id
        session["active_turn_id"] = turn_id
        session["pending_turn"] = {
            "turn_id": turn_id,
            "run_id": run_id,
            "client_message_id": client_message_id,
            "text": text,
            "attachments": submitted_attachments,
            "draft_text": str(params.get("draft_text") or text or ""),
            "model": requested_model,
            "model_descriptor": model_descriptor,
            "doxie_product_context": doxie_product_context,
        }
        session["run_started_at"] = time.time()
        session["run_updated_at"] = session["run_started_at"]
        session["interrupted_run_id"] = ""
        session["interrupted_turn_id"] = ""

    if requested_model:
        try:
            _apply_model_switch(sid, session, requested_model)
            _set_session_model_descriptor(
                session,
                model_descriptor,
                clear_if_empty=True,
            )
        except Exception as e:
            with session["history_lock"]:
                session["running"] = False
                session["active_run_id"] = None
                session["active_turn_id"] = None
                session["pending_turn"] = None
            return _err(rid, 5001, f"model switch failed: {e}")
    elif has_model_descriptor:
        _set_session_model_descriptor(session, model_descriptor, clear_if_empty=True)

    _server._ensure_session_turn_toolsets(
        sid,
        session,
        params.get("enabled_toolsets") or params.get("enabledToolsets"),
    )
    _start_agent_build(sid, session)

    def run_after_agent_ready() -> None:
        err = _wait_agent(session, rid)
        if err:
            _emit(
                "error",
                sid,
                {
                    "message": err.get("error", {}).get(
                        "message", "agent initialization failed"
                    )
                },
            )
            with session["history_lock"]:
                session["running"] = False
                session["active_run_id"] = None
                session["active_turn_id"] = None
                session["pending_turn"] = None
            return
        try:
            _ensure_agent_runtime_current(sid, session)
        except Exception as e:
            _emit("error", sid, {"message": f"runtime auth rebind failed: {e}"})
            with session["history_lock"]:
                session["running"] = False
                session["active_run_id"] = None
                session["active_turn_id"] = None
                session["pending_turn"] = None
            return
        with session["history_lock"]:
            if (
                str(session.get("interrupted_run_id") or "") == run_id
                or str(session.get("interrupted_turn_id") or "") == turn_id
                or str(session.get("active_run_id") or "") != run_id
                or turn_id in set(session.get("recalled_turn_ids") or set())
            ):
                return
        turn_metadata = {
            "turn_id": turn_id,
            "run_id": run_id,
            "client_message_id": client_message_id,
            "attachments": submitted_attachments,
            "draft_text": str(params.get("draft_text") or text or ""),
            "model": requested_model,
            "model_descriptor": model_descriptor,
            "doxie_product_context": doxie_product_context,
        }
        _server._run_prompt_submit(rid, sid, session, text, submitted_images, turn_metadata)

    threading.Thread(target=run_after_agent_ready, daemon=True).start()
    return _ok(
        rid,
        {
            "status": "streaming",
            "run_id": run_id,
            "turn_id": turn_id,
            "client_message_id": client_message_id,
            "session_id": sid,
            "stored_session_id": str(session.get("session_key") or ""),
        },
    )


def _submitted_attachments(params: dict) -> list[dict]:
    raw_attachments = params.get("attachments")
    if not isinstance(raw_attachments, list):
        return []
    attachments: list[dict] = []
    for raw in raw_attachments:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or raw.get("fileName") or raw.get("file_name") or "").strip()
        item = {
            "id": str(raw.get("id") or raw.get("fileId") or raw.get("file_id") or "").strip(),
            "name": name,
            "fileName": name,
            "mimeType": str(raw.get("mimeType") or raw.get("mime_type") or "").strip(),
            "size": raw.get("size") or raw.get("sizeBytes") or raw.get("size_bytes") or 0,
            "path": str(raw.get("path") or raw.get("localPath") or raw.get("local_path") or "").strip(),
            "fileUrl": str(raw.get("fileUrl") or raw.get("file_url") or raw.get("remoteUrl") or raw.get("remote_url") or "").strip(),
            "previewUrl": str(raw.get("previewUrl") or raw.get("preview_url") or raw.get("url") or "").strip(),
            "kind": str(raw.get("kind") or "").strip(),
        }
        attachments.append({
            k: v for k, v in item.items()
            if v is not None and not (isinstance(v, str) and v == "")
        })
    return attachments


def _submitted_image_paths(params: dict) -> list[str]:
    raw_attachments = params.get("attachments")
    if not isinstance(raw_attachments, list):
        return []
    image_paths: list[str] = []
    image_extensions = {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".bmp",
        ".tif",
        ".tiff",
        ".heic",
        ".heif",
    }
    for raw in raw_attachments:
        if not isinstance(raw, dict):
            continue
        raw_path = str(raw.get("path") or "").strip()
        if not raw_path:
            continue
        mime_type = str(raw.get("mimeType") or raw.get("mime_type") or "").lower()
        kind = str(raw.get("kind") or "").lower()
        path = Path(raw_path).expanduser()
        is_image = (
            kind == "image"
            or mime_type.startswith("image/")
            or path.suffix.lower() in image_extensions
        )
        if not is_image:
            continue
        if not path.is_file():
            print(
                f"[tui_gateway] prompt.submit skipped missing image attachment: {path}",
                file=sys.stderr,
                flush=True,
            )
            continue
        image_paths.append(str(path))
    return image_paths


def _run_prompt_submit(
    rid,
    sid: str,
    session: dict,
    text: Any,
    submitted_images: list[str] | None = None,
    turn_metadata: dict | None = None,
) -> None:
    with session["history_lock"]:
        history = list(session["history"])
        history_version = int(session.get("history_version", 0))
        images = [*list(session.get("attached_images", [])), *list(submitted_images or [])]
        turn_run_id = str(session.get("active_run_id") or "")
        turn_id = str((turn_metadata or {}).get("turn_id") or session.get("active_turn_id") or "")
        session["attached_images"] = []
    agent = session["agent"]
    _emit("message.start", sid)

    def is_turn_interrupted() -> bool:
        with session["history_lock"]:
            interrupted_run_id = str(session.get("interrupted_run_id") or "")
            interrupted_turn_id = str(session.get("interrupted_turn_id") or "")
            active_run_id = str(session.get("active_run_id") or "")
            if turn_run_id and interrupted_run_id == turn_run_id:
                return True
            if turn_id and interrupted_turn_id == turn_id:
                return True
            if turn_id and turn_id in set(session.get("recalled_turn_ids") or set()):
                return True
            return bool(turn_run_id and active_run_id and active_run_id != turn_run_id)

    delivered_parts: list[str] = []

    def persist_interrupted_partial() -> None:
        partial = "".join(delivered_parts).strip()
        if not partial:
            return
        assistant_message = {"role": "assistant", "content": partial}
        if turn_metadata:
            assistant_message["metadata"] = {
                "turn_id": turn_metadata.get("turn_id"),
                "run_id": turn_metadata.get("run_id"),
            }
        next_history = list(history)
        user_message = {"role": "user", "content": text}
        if turn_metadata:
            user_message["metadata"] = turn_metadata
        next_history.append(user_message)
        next_history.append(assistant_message)
        with session["history_lock"]:
            if int(session.get("history_version", 0)) != history_version:
                return
            session["history"] = next_history
            session["history_version"] = history_version + 1
        if hasattr(agent, "_persist_session"):
            if session.get("transient"):
                return
            try:
                agent._persist_session(next_history, conversation_history=history)
            except Exception as exc:
                print(
                    f"[tui_gateway] interrupted partial persistence failed sid={sid}: {exc}",
                    file=sys.stderr,
                    flush=True,
                )

    def run():
        approval_token = None
        session_tokens = []
        profile_tokens = []
        goal_followup = None  # set by the post-turn goal hook below
        try:
            profile_tokens = _enter_profile_context(session.get("profile_context"))
            from tools.approval import (
                reset_current_session_key,
                set_current_session_key,
            )

            approval_token = set_current_session_key(session["session_key"])
            session_cwd = _session_cwd(session)
            session_tokens = _set_session_context(
                session["session_key"],
                terminal_cwd=session_cwd,
                doxie_product_context=str((turn_metadata or {}).get("doxie_product_context") or ""),
            )
            cols = session.get("cols", 80)
            streamer = make_stream_renderer(cols)
            prompt = text

            if isinstance(prompt, str) and "@" in prompt:
                from agent.context_references import preprocess_context_references
                from agent.model_metadata import get_model_context_length

                ctx_len = get_model_context_length(
                    getattr(agent, "model", "") or _resolve_model(),
                    base_url=getattr(agent, "base_url", "") or "",
                    api_key=getattr(agent, "api_key", "") or "",
                    provider=getattr(agent, "provider", "") or "",
                    config_context_length=getattr(
                        agent, "_config_context_length", None
                    ),
                )
                ctx = preprocess_context_references(
                    prompt,
                    cwd=session_cwd,
                    allowed_root=session_cwd,
                    context_length=ctx_len,
                )
                if ctx.blocked:
                    _emit(
                        "error",
                        sid,
                        {
                            "message": "\n".join(ctx.warnings)
                            or "Context injection refused."
                        },
                    )
                    return
                prompt = ctx.message

            # Decide image routing per-turn based on active provider/model.
            # "native" → pass pixels to the main model as OpenAI-style content
            # parts (adapters translate for Anthropic/Gemini/Bedrock/etc.).
            # "text"   → pre-analyze with vision_analyze and prepend the text.
            # See agent/image_routing.py for the full decision table.
            run_message: Any = prompt
            if images:
                try:
                    from agent.image_routing import (
                        decide_image_input_mode,
                        build_native_content_parts,
                    )
                    from agent.auxiliary_client import (
                        _read_main_model,
                        _read_main_provider,
                    )
                    from hermes_cli.config import load_config as _tui_load_config

                    _cfg = _tui_load_config()
                    _descriptor = dict(session.get("model_descriptor") or {})
                    _supports_vision = (
                        bool(_descriptor.get("vision_enabled"))
                        if isinstance(_descriptor.get("vision_enabled"), bool)
                        else None
                    )
                    _mode = decide_image_input_mode(
                        _read_main_provider(),
                        _read_main_model(),
                        _cfg,
                        supports_vision_override=_supports_vision,
                    )
                except Exception as _img_exc:
                    print(
                        f"[tui_gateway] image_routing decision failed, defaulting to text: {_img_exc}",
                        file=sys.stderr,
                    )
                    _mode = "text"

                if _mode == "native":
                    try:
                        _parts, _skipped = build_native_content_parts(
                            prompt,
                            images,
                        )
                        if _skipped:
                            print(
                                f"[tui_gateway] native image attachment skipped {len(_skipped)} unreadable path(s)",
                                file=sys.stderr,
                            )
                        if any(p.get("type") == "image_url" for p in _parts):
                            run_message = _parts
                        else:
                            run_message = _enrich_with_attached_images(prompt, images)
                    except Exception as _img_exc:
                        print(
                            f"[tui_gateway] native attach failed, falling back to text: {_img_exc}",
                            file=sys.stderr,
                        )
                        run_message = _enrich_with_attached_images(prompt, images)
                else:
                    run_message = _enrich_with_attached_images(prompt, images)

            def _stream(delta):
                if is_turn_interrupted():
                    return
                payload = {"text": delta}
                if streamer and (r := streamer.feed(delta)) is not None:
                    payload["rendered"] = r
                delivered_parts.append(str(delta))
                _emit("message.delta", sid, payload)

            result = agent.run_conversation(
                run_message,
                conversation_history=list(history),
                stream_callback=_stream,
                turn_metadata=turn_metadata,
            )

            if is_turn_interrupted():
                persist_interrupted_partial()
                return

            last_reasoning = None
            status_note = None
            if isinstance(result, dict):
                if isinstance(result.get("messages"), list):
                    with session["history_lock"]:
                        current_version = int(session.get("history_version", 0))
                        if current_version == history_version:
                            session["history"] = result["messages"]
                            session["history_version"] = history_version + 1
                        else:
                            # History mutated externally during the turn
                            # (undo/compress/retry/rollback now guard on
                            # session.running, but this is the defensive
                            # backstop for any path that slips past).
                            # Surface the desync rather than silently
                            # dropping the agent's output — the UI can
                            # show the response and warn that it was
                            # not persisted.
                            print(
                                f"[tui_gateway] prompt.submit: history_version mismatch "
                                f"(expected={history_version} current={current_version}) — "
                                f"agent output NOT written to session history",
                                file=sys.stderr,
                            )
                            status_note = (
                                "History changed during this turn — the response above is visible "
                                "but was not saved to session history."
                            )

                # If auto-compression fired inside run_conversation(), agent.session_id
                # may have rotated. Sync session_key before downstream title/goal/finalize
                # handling uses it. Preserve pending_title (user intent) so it can be
                # applied to the continuation. Restart slash worker so subsequent
                # worker-backed commands (/title etc.) target the live session.
                # Fix for #20001.
                _sync_session_key_after_compress(
                    sid, session, clear_pending_title=False, restart_slash_worker=True,
                )

                raw = result.get("final_response", "")
                status = (
                    "interrupted"
                    if result.get("interrupted")
                    else "error" if result.get("error") else "complete"
                )
                # When the backend produced no visible response AND reported a
                # real error (e.g. invalid model slug → provider 4xx), surface
                # that error as the visible text instead of shipping an empty
                # turn to Ink. Mirrors classic CLI behavior at cli.py where
                # (failed|partial) + no final_response → "Error: <detail>".
                # Leaves the None-with-no-error path untouched: an empty
                # successful turn still renders as empty, and the existing
                # "(empty)" sentinel handling stays in its own lane.
                if (not raw) and result.get("error") and (
                    result.get("failed") or result.get("partial")
                ):
                    raw = f"Error: {result.get('error')}"
                lr = result.get("last_reasoning")
                if isinstance(lr, str) and lr.strip():
                    last_reasoning = lr.strip()
            else:
                raw = str(result)
                status = "complete"

            payload = {"text": raw, "usage": _get_usage(agent), "status": status}
            if last_reasoning:
                payload["reasoning"] = last_reasoning
            if status_note:
                payload["warning"] = status_note
            rendered = render_message(raw, cols)
            if rendered:
                payload["rendered"] = rendered
            _emit("message.complete", sid, payload)

            # ── /goal continuation (Ralph-style loop) ─────────────────
            # After every TUI turn, if a /goal is active, ask the judge
            # whether the goal is done and — if not and we're still under
            # budget — queue a continuation prompt to run after this
            # thread releases session["running"]. The verdict message
            # ("✓ Goal achieved" / "⏸ budget exhausted") is surfaced as
            # a system line so the user sees progress regardless of
            # outcome. Mirrors gateway/run._post_turn_goal_continuation.
            if status == "complete" and isinstance(raw, str) and raw.strip():
                try:
                    from hermes_cli.goals import GoalManager

                    sid_key = session.get("session_key") or ""
                    if sid_key:
                        try:
                            goals_cfg = _load_cfg().get("goals") or {}
                            goal_max_turns = int(goals_cfg.get("max_turns", 20) or 20)
                        except Exception:
                            goal_max_turns = 20
                        goal_mgr = GoalManager(
                            session_id=sid_key,
                            default_max_turns=goal_max_turns,
                        )
                        if goal_mgr.is_active():
                            decision = goal_mgr.evaluate_after_turn(
                                raw,
                                user_initiated=True,
                            )
                            verdict_msg = decision.get("message") or ""
                            if verdict_msg:
                                _emit(
                                    "status.update",
                                    sid,
                                    {"kind": "goal", "text": verdict_msg},
                                )
                            if decision.get("should_continue"):
                                cont_prompt = decision.get("continuation_prompt") or ""
                                if cont_prompt:
                                    goal_followup = cont_prompt
                except Exception as _goal_exc:
                    print(
                        f"[tui_gateway] goal continuation hook failed: "
                        f"{type(_goal_exc).__name__}: {_goal_exc}",
                        file=sys.stderr,
                    )

            # Apply pending_title now that the DB row exists.
            _pending = session.get("pending_title")
            if _pending and status == "complete":
                if session.get("transient"):
                    session["pending_title"] = None
                else:
                    _pdb = _get_db()
                    if _pdb:
                        _session_key = session.get("session_key") or sid
                        try:
                            if _pdb.set_session_title(_session_key, _pending):
                                session["pending_title"] = None
                        except ValueError as exc:
                            # Invalid/duplicate title — non-retryable, drop it.
                            # Auto-title will take over. Fix for #19029.
                            session["pending_title"] = None
                            logger.info(
                                "Dropping pending title for session %s: %s",
                                _session_key, exc,
                            )
                        except Exception:
                            # Transient DB failure — keep pending_title for retry.
                            pass

            if (
                status == "complete"
                and isinstance(raw, str)
                and raw.strip()
                and isinstance(text, str)
                and text.strip()
                and not session.get("transient")
            ):
                try:
                    from agent.title_generator import maybe_auto_title

                    maybe_auto_title(
                        _get_db(),
                        session.get("session_key") or sid,
                        text,
                        raw,
                        session.get("history", []),
                    )
                except Exception:
                    pass

            # CLI parity: when voice-mode TTS is on, speak the agent reply
            # (cli.py:_voice_speak_response).  Only the final text — tool
            # calls / reasoning already stream separately and would be
            # noisy to read aloud.
            if (
                status == "complete"
                and isinstance(raw, str)
                and raw.strip()
                and voice_tts_enabled()
            ):
                try:
                    from hermes_cli.voice import speak_text

                    spoken = raw
                    threading.Thread(
                        target=speak_text, args=(spoken,), daemon=True
                    ).start()
                except ImportError:
                    logger.warning("voice TTS skipped: hermes_cli.voice unavailable")
                except Exception as e:
                    logger.warning("voice TTS dispatch failed: %s", e)
        except Exception as e:
            import traceback

            trace = traceback.format_exc()
            try:
                os.makedirs(os.path.dirname(_CRASH_LOG), exist_ok=True)
                with open(_CRASH_LOG, "a", encoding="utf-8") as f:
                    f.write(
                        f"\n=== turn-dispatcher exception · "
                        f"{time.strftime('%Y-%m-%d %H:%M:%S')} · sid={sid} ===\n"
                    )
                    f.write(trace)
            except Exception:
                pass
            print(
                f"[gateway-turn] {type(e).__name__}: {e}", file=sys.stderr, flush=True
            )
            _emit("error", sid, {"message": str(e)})
        finally:
            try:
                if approval_token is not None:
                    reset_current_session_key(approval_token)
            except Exception:
                pass
            _clear_session_context(session_tokens)
            _leave_profile_context(profile_tokens)
            with session["history_lock"]:
                if str(session.get("active_run_id") or "") == turn_run_id:
                    session["running"] = False
                    session["active_run_id"] = None
                    session["active_turn_id"] = None
                    session["pending_turn"] = None
                    session["run_updated_at"] = time.time()

        # Chain a goal-continuation turn if the judge said so. We do
        # this AFTER the finally releases session["running"], so the
        # nested _run_prompt_submit doesn't deadlock on the busy
        # guard. A real user prompt that races us wins because
        # prompt.submit sets running=True under the history_lock and
        # we check that guard before re-firing.
        if goal_followup:
            with session["history_lock"]:
                if session.get("running"):
                    # User already sent something — their turn wins,
                    # the judge will re-run on the next turn anyway.
                    return
                session["running"] = True
                session["active_run_id"] = uuid.uuid4().hex
                session["active_turn_id"] = uuid.uuid4().hex
                session["run_started_at"] = time.time()
                session["run_updated_at"] = session["run_started_at"]
                session["interrupted_run_id"] = ""
                session["interrupted_turn_id"] = ""
            try:
                _emit("message.start", sid)
                _run_prompt_submit(rid, sid, session, goal_followup)
            except Exception as _cont_exc:
                print(
                    f"[tui_gateway] goal continuation dispatch failed: "
                    f"{type(_cont_exc).__name__}: {_cont_exc}",
                    file=sys.stderr,
                )
                with session["history_lock"]:
                    session["running"] = False
                    session["active_run_id"] = None

    threading.Thread(target=run, daemon=True).start()


@method("clipboard.paste")
def _(rid, params: dict) -> dict:
    session, err = _sess(params, rid)
    if err:
        return err
    try:
        from hermes_cli.clipboard import has_clipboard_image, save_clipboard_image
    except Exception as e:
        return _err(rid, 5027, f"clipboard unavailable: {e}")

    session["image_counter"] = session.get("image_counter", 0) + 1
    img_dir = Path(_active_hermes_home()) / "images"
    img_dir.mkdir(parents=True, exist_ok=True)
    img_path = (
        img_dir
        / f"clip_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{session['image_counter']}.png"
    )

    # Save-first: mirrors CLI keybinding path; more robust than has_image() precheck
    if not save_clipboard_image(img_path):
        session["image_counter"] = max(0, session["image_counter"] - 1)
        msg = (
            "Clipboard has image but extraction failed"
            if has_clipboard_image()
            else "No image found in clipboard"
        )
        return _ok(rid, {"attached": False, "message": msg})

    session.setdefault("attached_images", []).append(str(img_path))
    return _ok(
        rid,
        {
            "attached": True,
            "path": str(img_path),
            "count": len(session["attached_images"]),
            **_image_meta(img_path),
        },
    )


@method("image.attach")
def _(rid, params: dict) -> dict:
    session, err = _sess(params, rid)
    if err:
        return err
    raw = str(params.get("path", "") or "").strip()
    if not raw:
        return _err(rid, 4015, "path required")
    try:
        from cli import (
            _IMAGE_EXTENSIONS,
            _detect_file_drop,
            _resolve_attachment_path,
            _split_path_input,
        )

        dropped = _detect_file_drop(raw)
        if dropped:
            image_path = dropped["path"]
            remainder = dropped["remainder"]
        else:
            path_token, remainder = _split_path_input(raw)
            image_path = _resolve_attachment_path(path_token)
            if image_path is None:
                return _err(rid, 4016, f"image not found: {path_token}")
        if image_path.suffix.lower() not in _IMAGE_EXTENSIONS:
            return _err(rid, 4016, f"unsupported image: {image_path.name}")
        session.setdefault("attached_images", []).append(str(image_path))
        return _ok(
            rid,
            {
                "attached": True,
                "path": str(image_path),
                "count": len(session["attached_images"]),
                "remainder": remainder,
                "text": remainder or f"[User attached image: {image_path.name}]",
                **_image_meta(image_path),
            },
        )
    except Exception as e:
        return _err(rid, 5027, str(e))


@method("input.detect_drop")
def _(rid, params: dict) -> dict:
    session, err = _sess_nowait(params, rid)
    if err:
        return err
    try:
        from cli import _detect_file_drop

        raw = str(params.get("text", "") or "")
        dropped = _detect_file_drop(raw)
        if not dropped:
            return _ok(rid, {"matched": False})

        drop_path = dropped["path"]
        remainder = dropped["remainder"]
        if dropped["is_image"]:
            session.setdefault("attached_images", []).append(str(drop_path))
            text = remainder or f"[User attached image: {drop_path.name}]"
            return _ok(
                rid,
                {
                    "matched": True,
                    "is_image": True,
                    "path": str(drop_path),
                    "count": len(session["attached_images"]),
                    "text": text,
                    **_image_meta(drop_path),
                },
            )

        text = f"[User attached file: {drop_path}]" + (
            f"\n{remainder}" if remainder else ""
        )
        return _ok(
            rid,
            {
                "matched": True,
                "is_image": False,
                "path": str(drop_path),
                "name": drop_path.name,
                "text": text,
            },
        )
    except Exception as e:
        return _err(rid, 5027, str(e))


@method("prompt.background")
def _(rid, params: dict) -> dict:
    session, err = _sess(params, rid)
    if err:
        return err
    text, parent = params.get("text", ""), params.get("session_id", "")
    if not text:
        return _err(rid, 4012, "text required")
    task_id = f"bg_{uuid.uuid4().hex[:6]}"

    def run():
        session_tokens = _set_session_context(task_id, terminal_cwd=session.get("cwd"))
        try:
            from run_agent import AIAgent

            result = AIAgent(
                **_background_agent_kwargs(session["agent"], task_id)
            ).run_conversation(
                user_message=text,
                task_id=task_id,
            )
            _emit(
                "background.complete",
                parent,
                {
                    "task_id": task_id,
                    "text": (
                        result.get("final_response", str(result))
                        if isinstance(result, dict)
                        else str(result)
                    ),
                },
            )
        except Exception as e:
            _emit(
                "background.complete",
                parent,
                {"task_id": task_id, "text": f"error: {e}"},
            )
        finally:
            _clear_session_context(session_tokens)

    threading.Thread(target=run, daemon=True).start()
    return _ok(rid, {"task_id": task_id})


# ── Methods: respond ─────────────────────────────────────────────────


def _respond(rid, params, key):
    r = params.get("request_id", "")
    entry = _pending.get(r)
    if not entry:
        return _err(rid, 4009, f"no pending {key} request")
    _, ev = entry
    _answers[r] = params.get(key, "")
    ev.set()
    return _ok(rid, {"status": "ok"})


@method("clarify.respond")
def _(rid, params: dict) -> dict:
    return _respond(rid, params, "answer")


@method("sudo.respond")
def _(rid, params: dict) -> dict:
    return _respond(rid, params, "password")


@method("secret.respond")
def _(rid, params: dict) -> dict:
    return _respond(rid, params, "value")


@method("approval.respond")
def _(rid, params: dict) -> dict:
    session, err = _sess(params, rid)
    if err:
        return err
    try:
        from tools.approval import resolve_gateway_approval

        return _ok(
            rid,
            {
                "resolved": resolve_gateway_approval(
                    session["session_key"],
                    params.get("choice", "deny"),
                    resolve_all=params.get("all", False),
                )
            },
        )
    except Exception as e:
        return _err(rid, 5004, str(e))


@method("approval.policy.get")
def _(rid, params: dict) -> dict:
    session, err = _sess(params, rid)
    if err:
        return err
    try:
        from tools.approval import is_session_yolo_enabled

        yolo = is_session_yolo_enabled(session["session_key"])
        return _ok(
            rid,
            {
                "mode": "full_access" if yolo else "default",
                "yolo": yolo,
            },
        )
    except Exception as e:
        return _err(rid, 5004, str(e))


@method("approval.policy.set")
def _(rid, params: dict) -> dict:
    session, err = _sess(params, rid)
    if err:
        return err
    mode = str(params.get("mode") or "default").strip().lower()
    if mode not in {"default", "full_access"}:
        return _err(rid, 4002, f"unknown approval policy mode: {mode}")
    try:
        from tools.approval import disable_session_yolo, enable_session_yolo

        if mode == "full_access":
            enable_session_yolo(session["session_key"])
            yolo = True
        else:
            disable_session_yolo(session["session_key"])
            yolo = False
        return _ok(rid, {"mode": mode, "yolo": yolo})
    except Exception as e:
        return _err(rid, 5004, str(e))


@method("approval.pending.list")
def _(rid, params: dict) -> dict:
    session, err = _sess(params, rid)
    if err:
        return err
    try:
        from tools.approval import list_gateway_approvals

        return _ok(
            rid,
            {
                "approvals": list_gateway_approvals(session["session_key"]),
            },
        )
    except Exception as e:
        return _err(rid, 5004, str(e))
