const puppeteer=require('puppeteer-core'),path=require('path');
(async()=>{const b=await puppeteer.launch({executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',headless:'new'});
try{const p=await b.newPage();await p.goto(require('url').pathToFileURL(path.join(__dirname,'index.html')).href+'?render=1');
const r=await p.evaluate(([a,z])=>{const o=[];for(let t=a;t<=z+1e-9;t+=1/30){const R=remap(t);const S=scene(R.t);const s=S.snail;o.push([t.toFixed(3),R.t.toFixed(2),s?s.x.toFixed(0):'-',s?s.y.toFixed(0):'-',S.cam?`${S.cam.x.toFixed(0)},${S.cam.y.toFixed(0)},${S.cam.z.toFixed(2)}`:'nocam'].join(' '));}return o;},[+process.argv[2],+process.argv[3]]);
console.log(r.join('\n'));}finally{await b.close();}})();
