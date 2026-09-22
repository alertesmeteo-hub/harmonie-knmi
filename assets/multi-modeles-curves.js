(function () {
  "use strict";
  var colors={AROME:"#db2777",HARMONIE:"#0f766e",ARPEGE_EU:"#7c3aed",ECMWF:"#ca8a04",AIFS:"#0891b2",GFS:"#ea580c",GEFS:"#059669"};
  function draw(root){
    fetch(root.dataset.indexUrl+"?_c="+Date.now(),{cache:"no-store"}).then(function(response){
      if(!response.ok)throw Error("HTTP "+response.status);
      return response.json();
    }).then(function(data){
      var series=(data.series||[]).filter(function(item){return item.status==="ok"&&item.points&&item.points.length;});
      var svg=root.querySelector(".js-multi-curves");
      if(!series.length||!svg)return;
      var all=[];
      series.forEach(function(item){item.points.forEach(function(point){all.push([+new Date(point.valid_utc),+point.total_mm]);});});
      var start=Math.min.apply(null,all.map(function(point){return point[0];}));
      var end=Math.max.apply(null,all.map(function(point){return point[0];}));
      var maximum=Math.max.apply(null,[1].concat(all.map(function(point){return point[1];})));
      var width=900,height=250,left=42,bottom=28;
      function x(value){return left+(value-start)/(end-start)*(width-left-8);}
      function y(value){return height-bottom-value/maximum*(height-bottom-16);}
      var markup="";
      for(var i=0;i<5;i++){var tick=maximum*i/4;markup+='<path d="M'+left+" "+y(tick)+"H"+(width-8)+'" stroke="#e5edf2"/><text x="'+(left-5)+'" y="'+(y(tick)+4)+'" text-anchor="end">'+tick.toFixed(0)+"</text>";}
      series.forEach(function(item){markup+='<polyline fill="none" stroke="'+(colors[item.id]||"#334155")+'" stroke-width="3" points="'+item.points.map(function(point){return x(+new Date(point.valid_utc)).toFixed(1)+","+y(+point.total_mm).toFixed(1);}).join(" ")+'"/>';});
      svg.setAttribute("viewBox","0 0 "+width+" "+height);svg.innerHTML=markup;
    });
  }
  function boot(){document.querySelectorAll(".am-multi").forEach(draw);}
  if(document.readyState==="loading")document.addEventListener("DOMContentLoaded",boot);else boot();
}());
