import w from "./worker.js";
const store=new Map();
const env={FFD:{get:async k=>store.get(k)??null,put:async(k,v)=>store.set(k,v),list:async({prefix})=>({keys:[...store.keys()].filter(k=>k.startsWith(prefix)).map(name=>({name}))})},ADMIN_SECRET:"s3cret",FOUNDING_CAP:"2"};
const H={"content-type":"application/json","Origin":"https://freefaxdesk.com","CF-Connecting-IP":"1.2.3.4"};
const post=(p,b)=>w.fetch(new Request("https://api.freefaxdesk.com"+p,{method:"POST",headers:H,body:JSON.stringify(b)}),env).then(async r=>[r.status,await r.json()]);
const get=p=>w.fetch(new Request("https://api.freefaxdesk.com"+p,{headers:H}),env);
const t=Date.now()-5000;
console.log(await post("/review",{name:"Pat Q",role:"Office Manager",office_type:"Dental",city_state:"Reno, NV",desks:"6",rating:5,quote:"Saves my staff so much time. Receiving is really powerful.",may_publish:true,email:"l@x.com",t}));
console.log("hp", await post("/review",{name:"x",quote:"spam spam spam spam",hp:"y",t}));
console.log("fast", await post("/review",{name:"x",quote:"spam spam spam spam",t:Date.now()}));
const id=[...store.keys()].find(k=>k.startsWith("review:")).slice(7);
const sig=(await import("node:crypto")).createHmac("sha256","s3cret").update("review:"+id).digest("hex").slice(0,32);
console.log("approve", (await get(`/admin/approve?kind=review&id=${id}&sig=${sig}`)).status, "badsig", (await get(`/admin/approve?kind=review&id=${id}&sig=00`)).status);
console.log("reviews", await (await get("/reviews")).json());
for (let i=0;i<3;i++){ const [s,b]=await post("/founding",{name:"A"+i,office:"O"+i,office_type:"Dental",email:`a${i}@x.com`,agree:true,provider:"RingCentral",t}); const fid=[...store.keys()].filter(k=>k.startsWith("founding:2")).pop().slice(9); const fs=(await import("node:crypto")).createHmac("sha256","s3cret").update("founding:"+fid).digest("hex").slice(0,32); await get(`/admin/approve?kind=founding&id=${fid}&sig=${fs}`); console.log("founding",i,s,b.waitlist, await (await get("/founding/count")).json()); }
console.log(await post("/subscribe",{email:"z@x.com",provider:"eFax",source:"home",t}), store.get("provider:efax"));
console.log("rate", (await post("/subscribe",{email:"z@x.com",t}))[0], (await post("/subscribe",{email:"z@x.com",t}))[0]);
