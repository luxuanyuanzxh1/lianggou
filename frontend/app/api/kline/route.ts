type Candle={date:string;open:number;close:number;high:number;low:number;volume:number;amount:number};
type EastmoneyResponse={data?:{name?:string;klines?:string[]}};

function groupKey(date:string,period:string){
  const d=new Date(`${date}T00:00:00Z`),y=d.getUTCFullYear(),m=d.getUTCMonth();
  if(period==="周线"){const day=(d.getUTCDay()+6)%7,monday=new Date(d);monday.setUTCDate(d.getUTCDate()-day);return monday.toISOString().slice(0,10)}
  if(period==="月线")return `${y}-${String(m+1).padStart(2,"0")}`;
  if(period==="季线")return `${y}-Q${Math.floor(m/3)+1}`;
  if(period==="年线")return String(y);
  return date;
}

function aggregate(rows:Candle[],period:string){
  if(period==="日线"||period==="分时")return rows;
  const groups=new Map<string,Candle[]>();
  for(const row of rows){const key=groupKey(row.date,period),group=groups.get(key)||[];group.push(row);groups.set(key,group)}
  return [...groups.values()].map(g=>({date:g.at(-1)!.date,open:g[0].open,close:g.at(-1)!.close,high:Math.max(...g.map(x=>x.high)),low:Math.min(...g.map(x=>x.low)),volume:g.reduce((s,x)=>s+x.volume,0),amount:g.reduce((s,x)=>s+x.amount,0)}));
}

export async function GET(req:Request){
  const url=new URL(req.url),code=(url.searchParams.get("code")||"600519").replace(/\D/g,"").slice(0,6),market=url.searchParams.get("market")==="0"?0:url.searchParams.get("market")==="2"?0:1,period=url.searchParams.get("period")||"日线",fast=url.searchParams.get("mode")==="fast",adjustment=url.searchParams.get("adjustment")||"前复权",fqt=adjustment==="后复权"?2:adjustment==="不复权"?0:1;
  try{
    const native=period==="周线"?{klt:102,lmt:fast?520:10000}:period==="月线"?{klt:103,lmt:fast?360:10000}:period==="季线"||period==="年线"?{klt:103,lmt:fast?360:10000}:{klt:101,lmt:fast?520:10000};
    const endpoint=`https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=${market}.${code}&klt=${native.klt}&fqt=${fqt}&lmt=${native.lmt}&end=20500101&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61`;
    const res=await fetch(endpoint,{headers:{"User-Agent":"Mozilla/5.0","Referer":"https://quote.eastmoney.com/"},cache:"no-store"});
    if(!res.ok)throw new Error(`upstream ${res.status}`);
    const json:EastmoneyResponse=await res.json() as EastmoneyResponse;
    if(!json.data?.klines)throw new Error("upstream returned no kline data");
    const rows:Candle[]=(json.data?.klines||[]).map((line:string)=>{const v=line.split(",");return{date:v[0],open:+v[1],close:+v[2],high:+v[3],low:+v[4],volume:+v[5],amount:+v[6]}});
    const candles=period==="季线"||period==="年线"?aggregate(rows,period):rows;
    return Response.json({candles,name:json.data?.name,period,adjustment,source:native.klt===101?"daily-native":native.klt===102?"weekly-native":native.klt===103&&period==="月线"?"monthly-native":"monthly-aggregated",delayed:true},{headers:{"Cache-Control":"public, max-age=60, s-maxage=300, stale-while-revalidate=86400"}});
  }catch(error){return Response.json({candles:[],period,delayed:true,error:error instanceof Error?error.message:"行情源暂时不可用"},{status:502,headers:{"Cache-Control":"no-store"}})}
}
