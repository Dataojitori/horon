// 用法: node render.js [out.mp4] [--frames 0,30,60 --png dir]  (帧抽样模式)
const puppeteer = require('puppeteer-core');
const { spawn } = require('child_process');
const path = require('path'), fs = require('fs');
const EDGE = 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe';
(async () => {
  const args = process.argv.slice(2);
  const fi = args.indexOf('--times');
  const browser = await puppeteer.launch({ executablePath: EDGE, headless: 'new', args: ['--allow-file-access-from-files'] });
  const page = await browser.newPage();
  await page.setViewport({ width: 1920, height: 1080 });
  page.on('pageerror', e => console.error('PAGEERR', e.message));
  page.on('console', m => console.log('console:', m.text()));
  await page.goto(require('url').pathToFileURL(path.resolve(__dirname, 'index.html')).href + '?render=1');
  const grab = t => page.evaluate(t => { frame(t); return document.getElementById('c').toDataURL('image/jpeg', 0.94); }, t);
  if (fi >= 0) { // 抽帧
    const times = args[fi + 1].split(',').map(Number), dir = args[fi + 2] || 'frames';
    fs.mkdirSync(dir, { recursive: true });
    for (const t of times) { const d = await grab(t); fs.writeFileSync(path.join(dir, `f_${t.toFixed(2)}.jpg`), Buffer.from(d.split(',')[1], 'base64')); }
    await browser.close(); return;
  }
  const out = args[0] || 'snail.mp4';
  const DUR = await page.evaluate(() => DUR), FPS = 30, n = Math.round(DUR * FPS);
  const ff = spawn('ffmpeg', ['-y', '-f', 'image2pipe', '-framerate', String(FPS), '-c:v', 'mjpeg', '-i', '-', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-crf', '17', '-preset', 'slow', '-movflags', '+faststart', out], { stdio: ['pipe', 'ignore', 'inherit'] });
  const t0 = Date.now();
  for (let i = 0; i < n; i++) {
    const d = await grab(i / FPS);
    const buf = Buffer.from(d.split(',')[1], 'base64');
    if (!ff.stdin.write(buf)) await new Promise(r => ff.stdin.once('drain', r));
    if (i % 90 === 0) console.log(`frame ${i}/${n}  ${((Date.now() - t0) / 1000).toFixed(0)}s`);
  }
  ff.stdin.end(); await new Promise(r => ff.on('close', r));
  await browser.close(); console.log('done', out);
})();
