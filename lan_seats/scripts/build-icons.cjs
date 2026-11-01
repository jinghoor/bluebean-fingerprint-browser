// Inline only our vendored SVGs: the plugin is served as one client bundle.
const fs=require('node:fs'),path=require('node:path');
const root=path.resolve(__dirname,'..'),assets=path.join(root,'assets/platform-icons');
const manifest=JSON.parse(fs.readFileSync(path.join(assets,'manifest.json'),'utf8'));
const icons=Object.fromEntries(Object.entries(manifest).map(([key,entry])=>{
 const svg=fs.readFileSync(path.join(assets,entry.file),'utf8').trim();
 if(!svg.startsWith('<svg ')||/<(?:script|foreignObject|image|use)\b|\bon\w+\s*=|\bhref\s*=/i.test(svg))throw Error('Unexpected SVG content in '+entry.file);
 return[key,'data:image/svg+xml;charset=utf-8,'+encodeURIComponent(svg)];
}));
const client=path.join(root,'lib/client.js'),text=fs.readFileSync(client,'utf8');
const block='// BEGIN VENDORED PLATFORM ICONS\n    const PLATFORM_ICONS = '+JSON.stringify(icons,null,2).replace(/\n/g,'\n    ')+';\n    // END VENDORED PLATFORM ICONS';
const marker=/\/\/ BEGIN VENDORED PLATFORM ICONS[\s\S]*?\/\/ END VENDORED PLATFORM ICONS/;
if(!marker.test(text))throw Error('Missing client icon markers');
fs.writeFileSync(client,text.replace(marker,()=>block));
console.log('Embedded '+Object.keys(icons).length+' local icon mappings.');
