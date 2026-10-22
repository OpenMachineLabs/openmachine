"""CLI contracts stay offline; native media tests run when dependencies exist.

The repository's Python runtime does not install this standalone Skill's Node
package or system media tools. Native acceptance is also run explicitly before PR.
"""
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[3]
SKILL = ROOT / 'skill-library/article-motion'
CLI = SKILL / 'scripts/motion.cjs'
NODE = shutil.which('node')


@unittest.skipUnless(NODE, 'Node.js is required for the standalone Skill CLI')
class MotionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='article motion ')
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.film = self.directory / 'film.cjs'
        self.film.write_text("module.exports = {width: 64, height: 32, fps: 10, duration: 2, draw(ctx, t) {ctx.fillStyle = '#557788'; ctx.fillRect(0,0,64,32);}};\n")

    def call(self, *args, env=None):
        return subprocess.run([NODE, str(CLI), *map(str, args)], cwd=self.directory,
                              env={**os.environ, **(env or {})}, capture_output=True,
                              text=True, timeout=30)

    def assert_failure(self, result, message):
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn(message, result.stderr)

    def executable(self, name, body):
        p = self.directory / name
        p.write_text('#!/usr/bin/env python3\n' + textwrap.dedent(body))
        p.chmod(0o700)
        return str(p)

    def test_bad_time_scale_and_command_options_fail_before_rendering(self):
        for args, message in [
            (['still', self.film, '--at', '2', '--out', 'a.png'], 'inside [0, duration)'),
            (['render', self.film, '--start', '1.5', '--end', '1', '--out', 'a.mp4'], 'start < end'),
            (['render', self.film, '--start', '.01', '--end', '.02', '--out', 'a.mp4'], 'contains no frames'),
            (['still', self.film, '--scale', 'NaN', '--out', 'a.png'], 'finite number'),
            (['still', self.film, '--scale', '.1', '--out', 'a.png'], 'even integer dimensions'),
            (['still', self.film, '--end', '1', '--out', 'a.png'], 'not valid for still'),
        ]:
            with self.subTest(args=args):
                self.assert_failure(self.call(*args), message)
        self.assertFalse((self.directory / 'a.png').exists())
        self.assertFalse((self.directory / 'a.mp4').exists())

    def test_invalid_metadata_and_existing_output_are_not_silently_accepted(self):
        self.film.write_text('module.exports={width:63,height:32,fps:10,duration:2,draw(){}};')
        self.assert_failure(self.call('still', self.film, '--out', 'a.png'), 'positive even integer')
        self.film.write_text('module.exports={width:64,height:32,fps:10,duration:2,draw(){}};')
        target = self.directory / 'a.png'
        target.write_bytes(b'existing deliverable')
        self.assert_failure(self.call('still', self.film, '--out', target), 'Output exists')
        self.assertEqual(target.read_bytes(), b'existing deliverable')

    def test_inspect_rejects_audio_or_duration_and_does_not_claim_visual_review(self):
        metadata = {'streams': [{'codec_type': 'video', 'codec_name': 'h264',
                    'pix_fmt': 'yuv420p', 'width': 64, 'height': 32,
                    'avg_frame_rate': '10/1'}],
                    'format': {'duration': '2.0', 'format_name': 'mov,mp4,m4a,3gp,3g2,mj2'}}
        data = self.directory / 'metadata.json'
        probe = self.executable('probe', f'import pathlib\nprint(pathlib.Path({str(data)!r}).read_text())\n')
        decoder = self.executable('decoder', 'pass\n')
        env = {'FFPROBE': probe, 'FFMPEG': decoder}
        data.write_text(json.dumps(metadata))
        valid = self.call('inspect', 'film.mp4', '--under', '3', env=env)
        self.assertEqual(valid.returncode, 0, valid.stderr)
        report = json.loads(valid.stdout)
        self.assertTrue(report['checks']['decode'])
        self.assertEqual(report['semantic_review'], 'not_assessed')
        self.assertEqual(report['visual_review'], 'not_assessed')
        too_long = self.call('inspect', 'film.mp4', '--under', '2', env=env)
        self.assertNotEqual(too_long.returncode, 0)
        self.assertFalse(json.loads(too_long.stdout)['checks']['duration'])
        metadata['streams'].append({'codec_type': 'audio'})
        data.write_text(json.dumps(metadata))
        with_audio = self.call('inspect', 'film.mp4', env=env)
        self.assertNotEqual(with_audio.returncode, 0)
        self.assertFalse(json.loads(with_audio.stdout)['checks']['no_audio'])
        self.assertIsNone(json.loads(with_audio.stdout)['checks']['decode'])
        metadata['streams'] = []
        data.write_text(json.dumps(metadata))
        self.assertNotEqual(self.call('inspect', 'film.mp4', env=env).returncode, 0)

    def test_probe_and_decode_failure_are_errors(self):
        fail = self.executable('fail', "import sys\nsys.stderr.write('media failure')\nsys.exit(7)\n")
        self.assert_failure(self.call('inspect', 'missing.mp4', env={'FFPROBE': fail}), 'media failure')
        metadata = {'streams': [{'codec_type': 'video', 'codec_name': 'h264',
                    'pix_fmt': 'yuv420p', 'width': 64, 'height': 32, 'avg_frame_rate': '10/1'}],
                    'format': {'duration': '2', 'format_name': 'mp4'}}
        probe = self.executable('probe', 'print(' + repr(json.dumps(metadata)) + ')\n')
        self.assert_failure(self.call('inspect', 'broken.mp4', env={'FFPROBE': probe, 'FFMPEG': fail}), 'media failure')

    def native_dependencies(self):
        available = subprocess.run([NODE, '-e', "require('@napi-rs/canvas')"], cwd=SKILL,
                                   capture_output=True, timeout=15).returncode == 0
        if not available:
            self.skipTest('Run npm ci in the standalone article-motion Skill for native Canvas tests')
        return available

    def test_native_still_uses_requested_time_and_scale(self):
        self.native_dependencies()
        self.film.write_text('''module.exports={width:64,height:32,fps:10,duration:2,
          draw(ctx,t){if(t!==1.25)throw Error('wrong film time');ctx.fillStyle='#cd774e';ctx.fillRect(0,0,64,32);}};''')
        target = self.directory / 'frame.png'
        result = self.call('still', self.film, '--at', '1.25', '--scale', '.5', '--out', target)
        self.assertEqual(result.returncode, 0, result.stderr)
        png = target.read_bytes()
        self.assertEqual(png[:8], b'\x89PNG\r\n\x1a\n')
        self.assertEqual(struct.unpack('>II', png[16:24]), (32, 16))
        self.assertEqual(json.loads(result.stdout)['time'], 1.25)

    def test_native_failed_draw_preserves_existing_output_and_cleans_partial(self):
        self.native_dependencies()
        target = self.directory / 'frame.png'
        target.write_bytes(b'previous version')
        self.film.write_text('module.exports={width:64,height:32,fps:10,duration:2,draw(){throw Error("draw failed");}};')
        result = self.call('still', self.film, '--out', target, '--overwrite')
        self.assert_failure(result, 'draw failed')
        self.assertEqual(target.read_bytes(), b'previous version')
        self.assertEqual(list(self.directory.glob('.article-motion-*')), [])

    def test_native_clip_keeps_absolute_time_and_inspects_actual_media(self):
        self.native_dependencies()
        if not all(shutil.which(os.environ.get(key, default))
                   for key, default in [('FFMPEG', 'ffmpeg'), ('FFPROBE', 'ffprobe')]):
            self.skipTest('FFmpeg and FFprobe are required for the native media check')
        self.film.write_text('''module.exports={width:64,height:32,fps:10,duration:2,
          draw(ctx,t){if(t<1||t>=1.5)throw Error('clip restarted the timeline');
          if(ctx.globalAlpha!==1)throw Error('context leaked');
          ctx.fillStyle='#557788';ctx.fillRect(0,0,64,32);ctx.globalAlpha=.2;}};''')
        target = self.directory / 'clip.mp4'
        result = self.call('render', self.film, '--start', '1', '--end', '1.5', '--out', target)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['frames'], 5)
        checked = self.call('inspect', target, '--under', '1')
        self.assertEqual(checked.returncode, 0, checked.stderr)
        report = json.loads(checked.stdout)
        self.assertEqual((report['width'], report['height'], report['fps']), (64, 32, 10))
        self.assertAlmostEqual(report['duration'], .5, places=2)
        self.assertEqual(report['audio_streams'], 0)

    def test_native_encoder_failure_does_not_publish_output(self):
        self.native_dependencies()
        fail = self.executable('encoder-fail', 'import sys\nsys.stderr.write("encoder failed")\nsys.exit(3)\n')
        target = self.directory / 'film.mp4'
        result = self.call('render', self.film, '--out', target, env={'FFMPEG': fail})
        self.assert_failure(result, 'encoder failed')
        self.assertFalse(target.exists())
        self.assertEqual(list(self.directory.glob('.article-motion-*')), [])
        missing = self.call('render', self.film, '--out', target,
                            env={'FFMPEG': str(self.directory / 'missing-encoder')})
        self.assert_failure(missing, 'ENOENT')
        self.assertFalse(target.exists())


if __name__ == '__main__':
    unittest.main()
