from fractions import Fraction
from pathlib import Path
import unittest
import contextlib
import io
import json
import shutil
import tempfile
from unittest import mock
import vdls
from vdls_text_engine import (
    FontRequest, Paint, TextRequest, grapheme_clusters, layout_text,
    render_ass_surface,
)
from vdls_ffmpeg_backend import (
    FFmpegCapabilities, probe_ffmpeg, require_capabilities, validate_artifact,
)
from vdls_process import ProcessInterrupted, ProcessTimedOut, run_external
from vdls_media_metadata import exif_manifest_summary, read_exif
from vdls_subtitles import serialize_sidecar

EXAMPLE = Path("examples/hello.vdsl")
AUDIO_EXAMPLE = Path("examples/audio.vdsl")
ANIMATION_EXAMPLE = Path("examples/animation.vdsl")
SUBTITLE_EXAMPLE = Path("examples/subtitles.vdsl")
LOCALIZATION_EXAMPLE = Path("examples/localization.vdsl")
IMPORT_EXAMPLE = Path("examples/import-demo.vdsl")
GENERATORS_EXAMPLE = Path("examples/generators.vdsl")
REPRODUCIBLE_EXAMPLE = Path("examples/reproducible.vdsl")
MEDIA_EFFECTS_EXAMPLE = Path("examples/media-effects.vdsl")
VIDEO_LAYERS_EXAMPLE = Path("examples/video-layers.vdsl")
COLOR_EXAMPLE = Path("examples/color-management.vdsl")
AUDIO_PROCESSING_EXAMPLE = Path("examples/audio-processing.vdsl")

class FrontendTests(unittest.TestCase):
    def compile_text(self, text: str):
        return vdls.compile_source(text, Path("test.vdsl"))

    def test_lang_line_and_project_are_required(self):
        with self.assertRaisesRegex(vdls.Diagnostic, "VDLS-READ-006"):
            self.compile_text('#lang racket\n(project (id "x"))')
        with self.assertRaisesRegex(vdls.Diagnostic, "VDLS-PARSE-001"):
            self.compile_text("#lang vdls\n")

    def test_units_are_canonical(self):
        cases = [
            ("2s", {"num": 2, "den": 1}),
            ("250ms", {"num": 1, "den": 4}),
            ("30f", {"num": 1, "den": 2}),
            ("50pct", {"num": 1, "den": 2, "unit": "ratio"}),
            ("48kHz", {"num": 48000, "den": 1, "unit": "Hz"}),
        ]
        for literal, expected in cases:
            with self.subTest(literal=literal):
                self.assertEqual(vdls.ratio(literal, Fraction(60)), expected)

    def test_duplicate_and_unresolved_names_are_rejected(self):
        with self.assertRaisesRegex(vdls.Diagnostic, "VDLS-NAME-002"):
            self.compile_text('#lang vdls\n(project (id "x") '
                              '(asset a (file "a.mp4")) (asset a (file "b.mp4")))')
        with self.assertRaisesRegex(vdls.Diagnostic, "VDLS-NAME-007"):
            self.compile_text('#lang vdls\n(project (id "x") '
                              '(scene s (duration 1s) '
                              '(layer 0 (video (asset-ref missing)))))')

    def test_graph_is_typed_acyclic_and_has_targets(self):
        ast = vdls.compile_source(EXAMPLE.read_text(encoding="utf-8"), EXAMPLE)
        value = vdls.graph(ast)
        self.assertEqual(value["graphVersion"], "1.0.0")
        self.assertEqual(value["targets"][0]["id"], "main")
        self.assertTrue(all("inputs" in node and "outputs" in node
                            for node in value["nodes"]))
        self.assertTrue(all({"fromNode", "fromPort", "toNode", "toPort"} <= edge.keys()
                            for edge in value["edges"]))
        vdls.validate_graph(value)

    def test_project_timeline_is_explicit_and_legacy_compatible(self):
        legacy=vdls.compile_source(
            EXAMPLE.read_text(encoding="utf-8"),EXAMPLE)
        self.assertFalse(legacy["node"]["timeline"]["explicit"])
        self.assertEqual(
            legacy["node"]["timeline"]["items"][0]["start"],
            {"num":0,"den":1})
        source=Path("examples/global-timeline.vdsl")
        ast=vdls.compile_source(source.read_text(encoding="utf-8"),source)
        timeline=ast["node"]["timeline"]
        self.assertTrue(timeline["explicit"])
        self.assertEqual(timeline["duration"],{"num":4,"den":1})
        self.assertEqual(
            [item["kind"] for item in timeline["items"]],
            ["ScenePlacement","ScenePlacement","GlobalLayer","GlobalLayer",
             "GlobalLayer"])
        value=vdls.graph(ast)
        timeline_nodes=[
            node for node in value["nodes"]
            if node["kind"]=="core/project-timeline"]
        self.assertEqual(len(timeline_nodes),1)
        self.assertEqual(len(timeline_nodes[0]["params"]["markers"]),2)
        self.assertIn(
            ("metadata","timeline-metadata"),
            {(port["name"],port["mediaType"])
             for port in timeline_nodes[0]["outputs"]})
        mux=next(node for node in value["nodes"]
                 if node["kind"]=="core/mux")
        self.assertIn(
            ("chapters","timeline-metadata"),
            {(port["name"],port["mediaType"]) for port in mux["inputs"]})
        self.assertTrue(any(
            target["outputSpec"].get("kind")=="subtitle-sidecar"
            for target in value["targets"]))
        vdls.validate_graph(value)
        plan=vdls.ffmpeg_plans(ast,source)[0]
        self.assertIn("-map_chapters",plan["argv"])
        self.assertIn("title=Opening",plan["chapterText"])
        flattened,_=vdls.output_timeline_scene(
            ast,ast["node"]["outputs"][0])
        subtitle=next(
            layer for layer in flattened["layers"]
            if layer["content"]["kind"]=="Subtitles")
        self.assertEqual(
            subtitle["content"]["track"]["cues"][0]["start"],
            {"num":1,"den":1})

    def test_stack_and_grid_are_canonical_then_target_resolved(self):
        source=Path("examples/layout-stack-grid.vdsl")
        ast=vdls.compile_source(
            source.read_text(encoding="utf-8"),source)
        layout=ast["node"]["scenes"][0]["layers"][1]["content"]
        self.assertEqual(layout["kind"],"LayoutGroup")
        self.assertEqual(layout["layoutKind"],"grid")
        self.assertEqual(layout["columns"],2)
        value=vdls.graph(ast)
        kinds={node["kind"] for node in value["nodes"]}
        self.assertIn("core/layout-grid",kinds)
        self.assertIn("core/layout-stack",kinds)
        resolved=vdls.resolve_layout_layers(
            ast["node"]["scenes"][0]["layers"],640,360)
        text_layers=[
            layer for layer in resolved
            if layer["content"]["kind"]=="Text"]
        self.assertEqual(len(text_layers),5)
        rectangles=[
            layer["content"]["annotations"]["vdls.layoutRect"]
            for layer in text_layers]
        self.assertEqual(rectangles[0]["x"],{"num":24,"den":1})
        self.assertNotEqual(rectangles[0],rectangles[1])
        plan=vdls.ffmpeg_plans(ast,source)[0]
        self.assertNotIn("layout-",plan["filterScript"])

    def test_lisp_core_expands_functions_lists_for_and_component(self):
        source=Path("examples/lisp-core.vdsl")
        ast=vdls.compile_source(
            source.read_text(encoding="utf-8"),source)
        scene=ast["node"]["scenes"][0]
        self.assertEqual(len(scene["layers"]),6)
        labels=[
            layer["content"]["content"]["value"]
            for layer in scene["layers"]
            if layer["content"]["kind"]=="Text"]
        self.assertEqual(
            labels,["LAMBDA","LET","FOR","sum-of-squares=14",
                    "NAMESPACED MODULE"])
        positions=[
            layer["content"]["layout"]["position"][0]
            for layer in scene["layers"][1:4]]
        self.assertEqual(positions,["24px","224px","424px"])
        identity=ast["node"]["annotations"]["vdls.lispExpansion"]
        self.assertEqual(
            [name for name in identity["definitions"]
             if not name.startswith("__module_")],
            ["margin","squares","total","x-position"])
        self.assertEqual(
            identity["components"],["label","ui:module-label"])
        plan=vdls.ffmpeg_plans(ast,source)[0]
        transparent_inputs=[
            value for value in plan["argv"]
            if isinstance(value,str)
            and "color=c=black@0.0" in value]
        self.assertEqual(len(transparent_inputs),5)
        self.assertTrue(all(
            "colorchannelmixer=aa=0" in value
            for value in transparent_inputs))

    def test_lisp_parallel_lengths_and_dimension_errors_are_stable(self):
        with self.assertRaisesRegex(vdls.Diagnostic,"VDLS-LISP-021"):
            self.compile_text(
                '#lang vdls\n'
                '(define bad (map (lambda (x y) (+ x y)) '
                '(range 2) (range 3)))\n'
                '(project (id "x"))')
        with self.assertRaisesRegex(vdls.Diagnostic,"VDLS-TYPE-004"):
            self.compile_text(
                '#lang vdls\n'
                '(define bad (+ 1s 2px))\n'
                '(project (id "x"))')
        with self.assertRaisesRegex(vdls.Diagnostic,"VDLS-LISP-002"):
            self.compile_text(
                '#lang vdls\n'
                '(component badge ([x : Length]) (text "x" (position x 0px)))'
                '(project (id "x") (scene s (duration 1s) '
                '(layer 0 (badge (x "wrong")))))')

    def test_typed_component_slot_expands_before_ast(self):
        ast=self.compile_text(
            '#lang vdls\n'
            '(component card ([title : String]) '
            '(slot body : NodeList<Visual>) '
            '(group (text title) (slot-ref body))) '
            '(project (id "slot") '
            '(scene s (duration 1s) '
            '(layer 0 (card (title "Card") '
            '(body (text "A") (text "B"))))))')
        group=ast["node"]["scenes"][0]["layers"][0]["content"]
        self.assertEqual(group["kind"],"Group")
        self.assertEqual(
            [child["content"]["value"] for child in group["children"]],
            ["Card","A","B"])
        typed=self.compile_text(
            '#lang vdls\n'
            '(define result '
            '((lambda ([x : Number] [y : Number]) : Number (+ x y)) 2 3)) '
            '(project (id "typed"))')
        self.assertIn(
            "result",
            typed["node"]["annotations"]["vdls.lispExpansion"][
                "definitions"])
        with self.assertRaisesRegex(vdls.Diagnostic,"VDLS-LISP-002"):
            self.compile_text(
                '#lang vdls\n'
                '(define bad '
                '((lambda ([x : Length]) : Length x) 3)) '
                '(project (id "typed"))')

    def test_color_management_is_explicit_and_lowered(self):
        ast=vdls.compile_source(
            COLOR_EXAMPLE.read_text(encoding="utf-8"),COLOR_EXAMPLE)
        self.assertEqual(
            ast["node"]["colorManagement"]["workingSpace"]["transfer"],
            "linear")
        value=vdls.graph(ast)
        self.assertIn(
            "core/color-convert",
            {node["kind"] for node in value["nodes"]})
        plan=vdls.ffmpeg_plans(ast,COLOR_EXAMPLE)[0]
        self.assertIn("colorspace=iall=bt709",plan["filterScript"])

    def test_preview_profile_overrides_only_selected_video_targets(self):
        ast=vdls.compile_source(
            COLOR_EXAMPLE.read_text(encoding="utf-8"),COLOR_EXAMPLE)
        vdls.apply_preview_profile(ast,"320x180",["main"])
        output=ast["node"]["outputs"][0]
        self.assertEqual(
            (output["video"]["width"],output["video"]["height"]),(320,180))
        self.assertEqual(
            output["metadata"]["vdls.preview"],"non-conformant")
        self.assertIn("non-conformant",output["metadata"]["comment"])
        with self.assertRaisesRegex(vdls.Diagnostic,"VDLS-CLI-007"):
            vdls.apply_preview_profile(ast,"320-by-180",[])

    def test_preview_watch_snapshot_tracks_inputs_not_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            source=root/"main.vdsl"
            source.write_text("#lang vdls\n",encoding="utf-8")
            ignored=root/".vdls"/"preview.mp4"
            ignored.parent.mkdir()
            ignored.write_bytes(b"output")
            before=vdls.preview_watch_snapshot(root)
            self.assertIn(str(source.resolve()),before)
            self.assertNotIn(str(ignored.resolve()),before)
            source.write_text("#lang vdls\n; changed\n",encoding="utf-8")
            self.assertNotEqual(before,vdls.preview_watch_snapshot(root))

    def test_hdr_requires_explicit_tone_mapping(self):
        source='''#lang vdls
(project (id "hdr")
  (asset hdr (file "hdr.mp4")
    (color (primaries bt2020) (transfer pq)
           (matrix bt2020-ncl) (range limited)))
  (output (id "main") (file "hdr-out.mp4")
          (video (size 640 360) (fps 30)))
  (scene s (duration 1s) (layer 0 (video (asset-ref hdr)))))'''
        with self.assertRaisesRegex(vdls.Diagnostic,"VDLS-COLOR-003"):
            self.compile_text(source)

    def test_standard_audio_filters_lower_in_declared_order(self):
        ast=vdls.compile_source(
            AUDIO_PROCESSING_EXAMPLE.read_text(encoding="utf-8"),
            AUDIO_PROCESSING_EXAMPLE)
        script=vdls.ffmpeg_plans(ast,AUDIO_PROCESSING_EXAMPLE)[0][
            "filterScript"]
        names=[
            "highpass=","lowpass=","equalizer=","acompressor=",
            "alimiter=","loudnorm=",
        ]
        positions=[script.index(name) for name in names]
        self.assertEqual(positions,sorted(positions))

    def test_sidechain_duck_resolves_target_and_timeline_placement(self):
        source=Path("examples/sidechain-ducking.vdsl")
        ast=vdls.compile_source(source.read_text(encoding="utf-8"),source)
        plan=vdls.ffmpeg_plans(ast,source,"build",[])[0]
        script=plan["filterScript"]
        self.assertIn("adelay=1000:all=1",script)
        self.assertIn("apad=whole_dur=3",script)
        self.assertIn("sidechaincompress=",script)
        self.assertNotIn("__vdls_duck",script)

    def test_text_shadow_blur_stays_in_text_engine(self):
        font=next(Path("C:/Windows/Fonts").glob("arial.ttf"))
        request=TextRequest(
            "Shadow",FontRequest(str(font),"Arial",48),
            Paint(shadow="#00000080",shadow_x=2,shadow_y=6,shadow_blur=10),
            640,360)
        with tempfile.TemporaryDirectory() as directory:
            surface=render_ass_surface(
                layout_text(request),Path(directory))
            script=surface.ass_path.read_text(encoding="utf-8-sig")
        self.assertIn(r"\pos(2,6)",script)
        self.assertIn(r"\blur10",script)
        self.assertIn("Dialogue: 1,",script)

    def test_canonical_json_is_stable(self):
        value = {"z": 1, "a": {"b": 2}}
        self.assertEqual(vdls.canonical_json(value), '{"a":{"b":2},"z":1}\n')

    def test_expression_language_is_whitelisted(self):
        expression = vdls.normalize_expression(
            [vdls.Symbol("clamp"), vdls.Symbol("t"), vdls.Symbol("0"),
             vdls.Symbol("1")]
        )
        self.assertEqual(expression["operator"], "clamp")
        with self.assertRaisesRegex(vdls.Diagnostic, "VDLS-NAME-001"):
            vdls.normalize_expression([vdls.Symbol("system"), "rm"])

    def test_ffmpeg_expression_mapping(self):
        expression = [
            vdls.Symbol("*"), vdls.Symbol("1000"),
            [vdls.Symbol("clamp"),
             [vdls.Symbol("/"), vdls.Symbol("t"), vdls.Symbol("2")],
             vdls.Symbol("0"), vdls.Symbol("1")],
        ]
        self.assertEqual(
            vdls.compile_ffexpr(expression),
            "(1000*clip((t/2),0,1))",
        )

    def test_cli_reports_stable_usage_diagnostic(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            status = vdls.main(["--unknown"])
        self.assertEqual(status, 2)
        self.assertIn("VDLS-CLI-001", stderr.getvalue())
        self.assertEqual(vdls.diagnostic_exit_status("VDLS-FFMPEG-004"),11)
        self.assertEqual(vdls.diagnostic_exit_status("VDLS-PLUGIN-008"),124)
        self.assertEqual(vdls.diagnostic_exit_status("VDLS-PLUGIN-006"),12)
        stdout=io.StringIO()
        with contextlib.redirect_stdout(stdout):
            status=vdls.main([
                "--diagnostic-format","json","check",
                "definitely-not-a-project.vdsl",
            ])
        result=json.loads(stdout.getvalue())
        self.assertEqual(status,2)
        self.assertEqual(result["schema"],"vdls.cli-result/1")
        self.assertFalse(result["success"])
        self.assertEqual(len(result["diagnostics"]),1)

    def test_project_discovery_precedence(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/"main.vdsl").write_text("#lang vdls\n",encoding="utf-8")
            (root/"project.vdsl").write_text("#lang vdls\n",encoding="utf-8")
            self.assertEqual(vdls.discover(str(root)),(root/"project.vdsl").resolve())
            (root/"custom.vdsl").write_text("#lang vdls\n",encoding="utf-8")
            (root/"vdls.toml").write_text(
                'entry = "custom.vdsl"\n',encoding="utf-8")
            self.assertEqual(vdls.discover(str(root)),(root/"custom.vdsl").resolve())

    def test_build_manifest_is_deterministic_and_records_sources(self):
        first=vdls.build_manifest_document(
            EXAMPLE,EXAMPLE.read_text(encoding="utf-8"),None,[],[],[])
        second=vdls.build_manifest_document(
            EXAMPLE,EXAMPLE.read_text(encoding="utf-8"),None,[],[],[])
        self.assertEqual(vdls.canonical_json(first),vdls.canonical_json(second))
        self.assertEqual(first["schema"],"vdls.build-manifest/1")
        self.assertEqual(first["sources"][0]["path"],str(EXAMPLE.resolve()))

    def test_reproducible_mode_is_strict_and_deterministic(self):
        lock_path=Path("examples/vdls.lock").resolve()
        lock=json.loads(lock_path.read_text(encoding="utf-8"))
        backend=lock["backends"][0]
        capability={
            "version":backend["version"],
            "digest":backend["capabilityDigest"],
        }
        ast=vdls.compile_source(
            REPRODUCIBLE_EXAMPLE.read_text(encoding="utf-8"),
            REPRODUCIBLE_EXAMPLE)
        plans=vdls.ffmpeg_plans(
            ast,REPRODUCIBLE_EXAMPLE,"build",[],reproducible=True)
        for plan in plans: plan["backendCapabilities"]=capability
        evidence=vdls.reproducibility_evidence(
            ast,REPRODUCIBLE_EXAMPLE,plans,lock_path)
        self.assertEqual(evidence["seed"],2026)
        self.assertIn("-map_metadata",plans[0]["argv"])
        self.assertIn("threads=1:lookahead_threads=1",plans[0]["argv"])
        text_ast=vdls.compile_source(
            EXAMPLE.read_text(encoding="utf-8"),EXAMPLE)
        text_plans=vdls.ffmpeg_plans(
            text_ast,EXAMPLE,"build",[],reproducible=True)
        for plan in text_plans: plan["backendCapabilities"]=capability
        with self.assertRaisesRegex(
                vdls.Diagnostic,"pinned font asset"):
            vdls.reproducibility_evidence(
                text_ast,EXAMPLE,text_plans,lock_path)

    def test_process_supervisor_maps_interrupt_and_timeout(self):
        with mock.patch.object(vdls,"discover",side_effect=ProcessInterrupted()):
            stderr=io.StringIO()
            with contextlib.redirect_stderr(stderr):
                self.assertEqual(vdls.main(["check","anything.vdsl"]),130)
        with self.assertRaises(ProcessTimedOut):
            run_external([
                vdls.sys.executable,"-c","import time; time.sleep(2)"
            ],timeout=0.05,grace=0.05)

    def test_audio_graph_contains_required_pipeline(self):
        ast = vdls.compile_source(
            AUDIO_EXAMPLE.read_text(encoding="utf-8"), AUDIO_EXAMPLE)
        value = vdls.graph(ast)
        kinds = {node["kind"] for node in value["nodes"]}
        self.assertTrue({
            "core/generate-audio", "core/resample-audio",
            "core/encode-audio", "core/mux",
        } <= kinds)
        vdls.validate_graph(value)

    def test_configuration_and_lockfile(self):
        config = vdls.load_config(EXAMPLE)
        self.assertEqual(config["build"]["jobs"], 4)
        lock_path, digest = vdls.validate_lockfile(EXAMPLE.parent, config)
        self.assertEqual(lock_path.name, "vdls.lock")
        self.assertTrue(digest.startswith("sha256:"))

    def test_typed_template_expands_before_ast(self):
        ast = self.compile_text(
            '#lang vdls\n'
            '(define-template title-scene '
            '((name String) (length Duration 2s)) '
            '(scene generated (duration length) (layer 1 (text name))))\n'
            '(project (id "templates") '
            '(instantiate title-scene (name "Expanded")))'
        )
        scene = ast["node"]["scenes"][0]
        self.assertEqual(scene["sceneId"], "generated")
        self.assertEqual(scene["duration"], {"num": 2, "den": 1})
        self.assertEqual(
            scene["layers"][0]["content"]["content"]["value"], "Expanded")

    def test_animation_is_normalized_and_lowered(self):
        ast = vdls.compile_source(
            ANIMATION_EXAMPLE.read_text(encoding="utf-8"), ANIMATION_EXAMPLE)
        animation = ast["node"]["scenes"][0]["layers"][1]["content"]["animations"][0]
        self.assertEqual(animation["kind"], "FromTo")
        expression = vdls.compile_animation_ffexpr(animation)
        self.assertIn("clip((t-0)/2,0,1)", expression)
        self.assertIn("400", expression)

    def test_cubic_bezier_and_spring_are_normalized_and_lowered(self):
        source=Path("examples/advanced-easing.vdsl")
        ast=vdls.compile_source(source.read_text(encoding="utf-8"),source)
        animations=[
            layer["content"]["animations"][0]
            for layer in ast["node"]["scenes"][0]["layers"][1:]]
        self.assertEqual(animations[0]["easing"]["kind"],"CubicBezier")
        self.assertEqual(animations[1]["easing"]["kind"],"Spring")
        bezier=vdls.compile_animation_ffexpr(animations[0])
        spring=vdls.compile_animation_ffexpr(animations[1])
        self.assertIn("if(lt(",bezier)
        self.assertIn("cos(",spring)
        self.assertIn("exp(",spring)
        with self.assertRaisesRegex(vdls.Diagnostic,"VDLS-TYPE-009"):
            vdls.normalize_easing(["cubic-bezier","-0.1","0","0.5","1"])
        with self.assertRaisesRegex(vdls.Diagnostic,"VDLS-TYPE-009"):
            vdls.normalize_easing(["spring",["mass","0"]])

    def test_inline_karaoke_cues_lower_to_ass_and_export_plain_sidecar(self):
        source=Path("examples/karaoke.vdsl")
        ast=vdls.compile_source(source.read_text(encoding="utf-8"),source)
        subtitle=ast["node"]["scenes"][0]["layers"][1]["content"]
        cue=subtitle["track"]["cues"][0]
        self.assertEqual(cue["payload"]["kind"],"Karaoke")
        self.assertEqual(len(cue["payload"]["segments"]),3)
        plan=vdls.ffmpeg_plans(ast,source)[0]
        self.assertIn("ass=filename=",plan["filterScript"])
        self.assertEqual(
            vdls.serialize_sidecar(subtitle["track"]["cues"],"srt"),
            "1\n00:00:00,000 --> 00:00:02,000\nこんにちは\n")

    def test_r_is_a_deterministic_per_frame_random_variable(self):
        expression = vdls.compile_ffexpr(vdls.Symbol("r"))
        self.assertIn("n+0", expression)
        self.assertIn("mod(sin(", expression)
        self.assertEqual(expression, vdls.compile_ffexpr(vdls.Symbol("r")))
        seeded = vdls.frame_random_ffexpr(2026)
        self.assertIn("n+2026", seeded)
        variables={"__random_seed":"2026","__random_node":"99"}
        first=vdls.compile_ffexpr(
            [vdls.Symbol("random"),vdls.Symbol("3"),vdls.Symbol("7")],
            variables)
        self.assertEqual(
            first,vdls.compile_ffexpr(
                [vdls.Symbol("random"),vdls.Symbol("3"),vdls.Symbol("7")],
                variables))
        self.assertNotEqual(
            first,vdls.compile_ffexpr(
                [vdls.Symbol("random"),vdls.Symbol("4"),vdls.Symbol("7")],
                variables))
        component=vdls.compile_ffexpr([
            vdls.Symbol("component"),
            [vdls.Symbol("random3"),vdls.Symbol("3")],
            vdls.Symbol("2")],variables)
        self.assertNotEqual(first,component)
        with self.assertRaisesRegex(vdls.Diagnostic,"VDLS-LISP-060"):
            vdls.compile_ffexpr(
                [vdls.Symbol("random"),vdls.Symbol("not-an-index")],
                variables)

    def test_subtitles_are_normalized_to_half_open_cues(self):
        cues = vdls.parse_subtitles(
            Path("examples/captions.srt").read_text(encoding="utf-8"), ".srt")
        self.assertEqual(len(cues), 2)
        self.assertEqual(cues[0]["start"], {"num": 0, "den": 1})
        self.assertEqual(cues[0]["end"], cues[1]["start"])
        ast = vdls.compile_source(
            SUBTITLE_EXAMPLE.read_text(encoding="utf-8"), SUBTITLE_EXAMPLE)
        subtitle = ast["node"]["scenes"][0]["layers"][1]["content"]
        self.assertEqual(subtitle["kind"], "Subtitles")
        self.assertEqual(len(subtitle["track"]["cues"]), 2)
        self.assertEqual(
            subtitle["sidecar"],
            {"path":"captions-export.srt","format":"srt"})
        value=vdls.graph(ast)
        kinds={node["kind"] for node in value["nodes"]}
        self.assertIn("core/export-subtitles",kinds)
        self.assertTrue(any(
            target["outputSpec"].get("kind")=="subtitle-sidecar"
            for target in value["targets"]))
        serialized=serialize_sidecar(subtitle["track"]["cues"],"srt")
        self.assertIn("00:00:00,000 --> 00:00:01,000",serialized)
        self.assertEqual(serialized,serialize_sidecar(
            vdls.parse_subtitles(serialized,".srt"),"srt"))

    def test_exif_is_normalized_and_privacy_summarized(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"photo.jpg"
            image=Image.new("RGB",(8,6),(12,34,56))
            exif=Image.Exif()
            exif[271]="VDLS Camera"
            exif[272]="Reference One"
            exif[274]=6
            exif[36867]="2026:07:26 12:34:56"
            image.save(path,exif=exif)
            normalized=read_exif(path)
            self.assertEqual(normalized["tags"]["Make"],"VDLS Camera")
            self.assertEqual(normalized["tags"]["Orientation"],6)
            summary=exif_manifest_summary(normalized)
            self.assertEqual(summary["selected"]["Model"],"Reference One")
            self.assertNotIn("gps",summary)
            self.assertTrue(summary["digest"].startswith("sha256:"))

    def test_localization_resolves_before_ast(self):
        ast = vdls.compile_source(
            LOCALIZATION_EXAMPLE.read_text(encoding="utf-8"),
            LOCALIZATION_EXAMPLE)
        text = ast["node"]["scenes"][0]["layers"][1]["content"]["content"]
        self.assertEqual(text, {"kind":"Literal","value":"こんにちは、VDLS"})

    def test_explicit_import_resolves_template_module(self):
        ast = vdls.compile_source(
            IMPORT_EXAMPLE.read_text(encoding="utf-8"), IMPORT_EXAMPLE)
        self.assertEqual(ast["node"]["scenes"][0]["sceneId"], "imported-scene")
        self.assertEqual(ast["node"]["imports"][0]["module"], "title-module.vdsl")

    def test_plugin_manifest_and_lock_are_validated(self):
        manifest_path = Path("examples/plugins/demo/vdls-plugin.json")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        plugin = vdls.validate_plugin_manifest(manifest, manifest_path)
        self.assertEqual(plugin["abi"], "vdls.plugin/1")
        invalid = dict(manifest, abi="vdls.plugin/2")
        with self.assertRaisesRegex(vdls.Diagnostic, "VDLS-PLUGIN-003"):
            vdls.validate_plugin_manifest(invalid, manifest_path)
        config = vdls.load_config(EXAMPLE)
        plugins = vdls.load_locked_plugins(EXAMPLE.parent, config)
        self.assertEqual(plugins[0]["id"], "org.example.metadata")
        with vdls.PluginProcessHost(
                plugins[0], EXAMPLE.parent, EXAMPLE.parent / ".vdls/cache",
                set(plugins[0]["permissions"]), timeout=5) as host:
            self.assertEqual(host.capabilities(), ["analyzer.media"])
            result = host.invoke("analyzer.media", {"asset": "demo"})
            self.assertTrue(result["value"]["analyzed"])
            self.assertEqual(
                host.cancel("request-1"), {"cancelled": "request-1"})

    def test_standard_generators_and_presets_lower(self):
        ast = vdls.compile_source(
            GENERATORS_EXAMPLE.read_text(encoding="utf-8"),
            GENERATORS_EXAMPLE)
        plans = vdls.ffmpeg_plans(
            ast, GENERATORS_EXAMPLE, "build", [])
        self.assertEqual(len(plans), 3)
        inputs = "\n".join(" ".join(plan["argv"]) for plan in plans)
        self.assertIn("gradients=", inputs)
        self.assertIn("type=radial", inputs)
        self.assertIn("geq=", inputs)
        preset = self.compile_text(
            '#lang vdls\n(project (id "p") '
            '(output (id "o") (file "o.mp4") (preset preview-low)))')
        self.assertEqual(
            preset["node"]["outputs"][0]["video"]["width"], 640)

    def test_required_media_effects_lower_in_order(self):
        ast=vdls.compile_source(
            MEDIA_EFFECTS_EXAMPLE.read_text(encoding="utf-8"),
            MEDIA_EFFECTS_EXAMPLE)
        plan=vdls.ffmpeg_plans(ast,MEDIA_EFFECTS_EXAMPLE,"build",[])[0]
        script=plan["filterScript"]
        self.assertLess(script.index("crop="),script.index("pad="))
        self.assertIn("fade=t=in",script)
        self.assertIn("fade=t=out",script)
        self.assertIn("atempo=5/4",script)
        self.assertIn("afade=t=in",script)
        self.assertIn("afade=t=out",script)
        self.assertIn("title=VDLS Media Effects",plan["argv"])
        self.assertIn("artist=VDLS Reference",plan["argv"])

    def test_extended_visual_filters_validate_and_lower(self):
        filters=vdls.compile_visual_effects([
            ["temperature","800"],["tint","0.1"],
            ["color-matrix",
             ["1","0","0","0","0"],["0","1","0","0","0"],
             ["0","0","1","0","0"],["0","0","0","1","0"]],
            ["chroma-key","#00ff00",["similarity","0.1"],
             ["smoothness","0.05"],["spill","0.1"]],
            ["alpha-from-luma"],["reverse"],
            ["frame-rate","60",["mode","blend"]],
        ])
        script=",".join(filters)
        for name in (
                "colortemperature","colorbalance","geq=","chromakey",
                "despill","reverse","framerate"):
            self.assertIn(name,script)
        with self.assertRaisesRegex(vdls.Diagnostic,"VDLS-TYPE-009"):
            vdls.compile_visual_effects([["temperature","50000"]])
        marker=vdls.compile_visual_effects(
            [["freeze-frame","500ms","500ms"]],Fraction(30))
        self.assertEqual(marker,["__vdls_freeze__=15:29:15"])
        source=Path("examples/extended-filters.vdsl")
        ast=vdls.compile_source(source.read_text(encoding="utf-8"),source)
        plan=vdls.ffmpeg_plans(ast,source,"build",[])[0]
        self.assertIn("freezeframes=first=15:last=29:replace=15",
                      plan["filterScript"])

    def test_asset_mask_lowers_to_explicit_two_input_graph(self):
        source=Path("examples/asset-mask.vdsl")
        ast=vdls.compile_source(source.read_text(encoding="utf-8"),source)
        plan=vdls.ffmpeg_plans(ast,source,"build",[])[0]
        script=plan["filterScript"]
        self.assertIn("scale2ref",script)
        self.assertIn("format=gray",script)
        self.assertIn("alphamerge",script)
        self.assertNotIn("__vdls_mask",script)

    def test_multiple_video_layers_transform_and_blend(self):
        ast=vdls.compile_source(
            VIDEO_LAYERS_EXAMPLE.read_text(encoding="utf-8"),
            VIDEO_LAYERS_EXAMPLE)
        plan=vdls.ffmpeg_plans(ast,VIDEO_LAYERS_EXAMPLE,"build",[])[0]
        script=plan["filterScript"]
        self.assertIn("blend=all_mode=multiply",script)
        self.assertIn("rotate=angle=",script)
        self.assertIn("overlay_w/2",script)
        self.assertIn("overlay_h/2",script)

    def test_all_required_blend_modes_have_backend_mappings(self):
        required={
            "multiply","screen","overlay","darken","lighten","color-dodge",
            "color-burn","hard-light","soft-light","difference","exclusion",
        }
        self.assertTrue(all(vdls.ffmpeg_blend_mode(name)
                            for name in required))
        source=Path("examples/blend-modes.vdsl")
        ast=vdls.compile_source(source.read_text(encoding="utf-8"),source)
        script=vdls.ffmpeg_plans(ast,source,"build",[])[0]["filterScript"]
        self.assertIn("blend=all_mode=overlay",script)
        self.assertIn("blend=all_mode=difference",script)
        self.assertIn("blend=all_mode=softlight",script)
        self.assertIn("maskedmerge",script)
        self.assertIn("lutrgb=",script)
        self.assertIn("premultiply=planes=7",script)
        self.assertIn("unpremultiply=planes=7",script)

    def test_text_engine_is_independent_from_drawtext(self):
        ast=vdls.compile_source(EXAMPLE.read_text(encoding="utf-8"),EXAMPLE)
        plan=vdls.ffmpeg_plans(ast,EXAMPLE,"build",[])[0]
        self.assertNotIn("drawtext",plan["filterScript"])
        self.assertIn("shaping=complex",plan["filterScript"])
        self.assertIn("overlay=",plan["filterScript"])
        font=vdls._default_font_file("Hello")
        decomposed="Cafe\u0301"
        preserved=layout_text(TextRequest(
            decomposed,FontRequest(str(font),font.stem,32),Paint(),640,360))
        normalized=layout_text(TextRequest(
            decomposed,FontRequest(str(font),font.stem,32),Paint(),640,360,
            normalization="nfc"))
        self.assertEqual(preserved.normalized_text,decomposed)
        self.assertEqual(normalized.normalized_text,"Café")
        self.assertNotEqual(preserved.digest,normalized.digest)

    def test_text_layout_exposes_font_glyph_runs_before_rasterization(self):
        font=vdls._default_font_file("Glyph runs")
        layout=layout_text(TextRequest(
            "A\\u0301B",FontRequest(str(font),font.stem,32),Paint(),640,360,
            normalization="nfc"))
        self.assertEqual(len(layout.shaped_runs),1)
        run=layout.shaped_runs[0]
        self.assertEqual(len(run.glyphs),len(run.text))
        self.assertEqual(run.glyphs[0].cluster,(0,1))
        self.assertGreater(run.glyphs[0].glyph_id,0)
        self.assertGreater(run.glyphs[0].advance_x,0)
        self.assertEqual(layout.digest[:7],"sha256:")

    def test_typewriter_uses_extended_grapheme_clusters(self):
        text="A\u0301👨‍👩‍👧‍👦"
        self.assertEqual(grapheme_clusters(text),("A\u0301","👨‍👩‍👧‍👦"))
        font=vdls._default_font_file(text)
        layout=layout_text(TextRequest(
            text,FontRequest(str(font),font.stem,40),Paint(),640,360,
            anchor="center",typewriter_duration=(2,1)))
        with tempfile.TemporaryDirectory() as directory:
            surface=render_ass_surface(layout,Path(directory))
            ass=surface.ass_path.read_text(encoding="utf-8")
            self.assertEqual(ass.count("Dialogue:"),2)
            self.assertIn("0:00:01.00",ass)
            self.assertIn("0:00:02.00",ass)
        line_layout=layout_text(TextRequest(
            "one\ntwo",FontRequest(str(font),font.stem,40),Paint(),640,360,
            reveal_lines_duration=(2,1),fade_in_duration=(1,2)))
        with tempfile.TemporaryDirectory() as directory:
            ass=render_ass_surface(
                line_layout,Path(directory)).ass_path.read_text(encoding="utf-8")
            self.assertEqual(ass.count("Dialogue:"),2)
            self.assertIn(r"\fad(500,0)",ass)
            self.assertIn("one\\Ntwo",ass)
        highlight_layout=layout_text(TextRequest(
            "Build verify",FontRequest(str(font),font.stem,40),Paint(),640,360,
            word_highlights=((0,1,1,2),(1,2,1,1))))
        with tempfile.TemporaryDirectory() as directory:
            ass=render_ass_surface(
                highlight_layout,Path(directory)).ass_path.read_text(encoding="utf-8")
            self.assertIn(r"{\1c&H00FFFF&}Build",ass)
            self.assertIn(r"{\1c&H00FFFF&}verify",ass)
        box_layout=layout_text(TextRequest(
            "wrap these words",FontRequest(str(font),font.stem,40),Paint(),
            320,120,anchor="center",wrap_mode="word"))
        with tempfile.TemporaryDirectory() as directory:
            surface=render_ass_surface(box_layout,Path(directory))
            self.assertEqual((surface.anchor_x,surface.anchor_y),(160,60))
            self.assertIn(r"\q2",surface.ass_path.read_text(encoding="utf-8"))

    def test_text_box_wrap_and_overflow_modes_are_deterministic(self):
        font=vdls._default_font_file("alpha beta gamma")
        base=dict(
            content="alpha beta gamma delta epsilon",
            font=FontRequest(str(font),font.stem,40),paint=Paint(),
            frame_width=180,frame_height=90,max_lines=2,line_height=1.1)
        grapheme=layout_text(TextRequest(**base,wrap_mode="grapheme"))
        balanced=layout_text(TextRequest(**base,wrap_mode="balanced"))
        ellipsis=layout_text(TextRequest(
            **base,wrap_mode="word",overflow="ellipsis"))
        shrink=layout_text(TextRequest(
            **base,wrap_mode="word",overflow="shrink"))
        self.assertGreater(len(grapheme.lines),1)
        self.assertGreater(len(balanced.lines),1)
        self.assertTrue(ellipsis.normalized_text.endswith("…"))
        self.assertLess(shrink.font.size,40)
        self.assertLessEqual(len(shrink.lines),2)
        self.assertEqual(
            balanced.digest,
            layout_text(TextRequest(**base,wrap_mode="balanced")).digest)
        visible=layout_text(TextRequest(
            content="This extends beyond the box",
            font=FontRequest(str(font),font.stem,36),paint=Paint(
                shadow="#00000080",shadow_x=8,shadow_y=6,shadow_blur=4),
            frame_width=120,frame_height=50,wrap_mode="none",
            overflow="visible"))
        self.assertGreater(visible.frame_width,visible.box_width)
        self.assertGreater(visible.frame_height,visible.box_height)
        self.assertGreater(visible.anchor_x,0)

    def test_ffmpeg_capabilities_and_artifact_contract(self):
        executable=shutil.which("ffmpeg")
        if executable:
            capabilities=probe_ffmpeg(executable,vdls.Diagnostic)
            self.assertIn("overlay",capabilities.filters)
            self.assertIn("libx264",capabilities.encoders)
            self.assertIn("h264",capabilities.decoders)
            self.assertIn("yuv420p",capabilities.pixel_formats)
            self.assertTrue(capabilities.digest.startswith("sha256:"))
        unavailable=FFmpegCapabilities(
            "ffmpeg","test",(),(),(),(),"sha256:test")
        with self.assertRaisesRegex(vdls.Diagnostic,"VDLS-FFMPEG-004"):
            require_capabilities(
                unavailable,{"filters":["overlay"]},vdls.Diagnostic)
        with tempfile.TemporaryDirectory() as directory:
            artifact=Path(directory)/"result.mp4"
            artifact.write_bytes(b"not-empty")
            expected={
                "video":{"width":640,"height":360,
                         "frameRate":{"num":30,"den":1}},
                "audio":None,"duration":{"num":2,"den":1},
            }
            probe_data={
                "streams":[{"codec_type":"video","width":640,"height":360,
                            "r_frame_rate":"30/1"}],
                "format":{"duration":"2.000000"},
            }
            validate_artifact(artifact,probe_data,expected,vdls.Diagnostic)
            probe_data["streams"][0]["width"]=1280
            with self.assertRaisesRegex(vdls.Diagnostic,"VDLS-FFMPEG-012"):
                validate_artifact(artifact,probe_data,expected,vdls.Diagnostic)

if __name__ == "__main__":
    unittest.main()
