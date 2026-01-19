function s(t,a=!0){a?window.open(t):window.location.href=t}function d(t,a="HTTP/1.1"){if(!(t!=null&&t.status))return"No response";const i=`${a} ${t.status} ${t.statusText}`,n=Object.entries(t.headers).map(([u,o])=>`${u}: ${o}`).join(`\r
`),r=t.data?`\r
\r
${typeof t.data=="string"?JSON.stringify(JSON.parse(t.data),null,2):JSON.stringify(t.data,null,2)}`:"";return`${i}\r
${n}${r}`}const f=t=>/^(http|https|file):\/\/.*/.test(t);export{f as i,s as o,d as p};
