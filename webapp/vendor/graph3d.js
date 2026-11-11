/* Batched WebGL graph renderer. UI state remains owned by index.html. */
function buildGraph() {
  const T=window.THREE, host=$('graph');
  let renderer;
  try { renderer=new T.WebGLRenderer({antialias:true}); }
  catch(e) { throw new Error('WebGL недоступен. Включите аппаратное ускорение или откройте граф в другом браузере.'); }
  renderer.setPixelRatio(Math.min(devicePixelRatio,1.5)); host.append(renderer.domElement);
  const canvas=renderer.domElement, scene=new T.Scene(); scene.background=new T.Color(COL.bg);
  const camera=new T.PerspectiveCamera(45,1,0.1,100000), target=new T.Vector3();
  let radius=500, theta=0.3, phi=1.2, generation=0, worker=null, mesh=null, lines=null, dirty=true, colorsDirty=true;
  const pointers=new Map(); let gesture=null, suppressClick=false;
  const labels=document.createElement('canvas'); Object.assign(labels.style,{position:'absolute',inset:'0',pointerEvents:'none'}); host.append(labels);
  const ctx=labels.getContext('2d'), dummy=new T.Object3D(), color=new T.Color(), projected=new T.Vector3();
  const newColor=new T.Color(COL.new), touchedColor=new T.Color(COL.touched);
  let lastState=[];
  function invalidate(colors=false){dirty=true;if(colors)colorsDirty=true;}
  const api={
    width(){resize();return api;},height(){resize();return api;},
    graphData(){rebuild();return api;},
    layout(spread){
      if(worker) worker.terminate();
      const current=new Worker('/vendor/layout-worker.js'), gen=++generation; worker=current;
      const nodes=S.nodes, index=new Map(nodes.map((n,i)=>[n.id,i]));
      current.onmessage=({data})=>{if(data.generation!==generation||worker!==current)return;
        nodes.forEach((n,i)=>{n.x=data.positions[i*3];n.y=data.positions[i*3+1];n.z=data.positions[i*3+2];});
        updateGeometry(); if(!S._userMoved) api.zoomToFit(); current.terminate(); worker=null;
      };
      current.onerror=()=>{if(worker!==current)return;current.terminate();worker=null;$('overlay').style.display='flex';$('overlay-text').textContent='Не удалось выполнить 3D-компоновку.';document.querySelector('.spin').style.display='none';};
      current.postMessage({nodes:nodes.map(n=>({community:n.community})),edges:S.links.map(l=>[index.get(l.source.id),index.get(l.target.id)]),spread,generation:gen});
      return api;
    },
    zoomToFit(ms,pad,filter){const ns=filter?S.nodes.filter(filter):S.nodes;if(!ns.length)return api;
      const box=new T.Box3(); for(const n of ns) box.expandByPoint(new T.Vector3(n.x||0,n.y||0,n.z||0));
      box.getCenter(target); radius=Math.max(100,box.getSize(new T.Vector3()).length()*1.3/Math.min(1,camera.aspect)); invalidate(true);return api;},
    focus(n){target.set(n.x||0,n.y||0,n.z||0);radius=Math.max(80,Math.min(radius,220));invalidate(true);return api;}
  };
  function resize(){const w=host.clientWidth||innerWidth,h=host.clientHeight||innerHeight;if(labels.width===w&&labels.height===h)return;renderer.setSize(w,h);camera.aspect=w/h;camera.updateProjectionMatrix();labels.width=w;labels.height=h;invalidate();}
  function rebuild(){
    if(mesh){scene.remove(mesh);mesh.geometry.dispose();mesh.material.dispose();}
    if(lines){scene.remove(lines);lines.geometry.dispose();lines.material.dispose();}
    mesh=new T.InstancedMesh(new T.SphereGeometry(1,8,6),new T.MeshBasicMaterial(),S.nodes.length);mesh.frustumCulled=false;scene.add(mesh);
    const geometry=new T.BufferGeometry();geometry.setAttribute('position',new T.BufferAttribute(new Float32Array(S.links.length*6),3));geometry.setAttribute('color',new T.BufferAttribute(new Float32Array(S.links.length*6),3));
    lines=new T.LineSegments(geometry,new T.LineBasicMaterial({vertexColors:true,transparent:true,opacity:.45}));scene.add(lines);
    updateGeometry();invalidate(true);api.layout(S.spread);
  }
  function updateGeometry(){
    S.nodes.forEach((n,i)=>{dummy.position.set(n.x||0,n.y||0,n.z||0);dummy.scale.setScalar(n.r||1);dummy.updateMatrix();mesh.setMatrixAt(i,dummy.matrix);});mesh.instanceMatrix.needsUpdate=true;
    const pos=lines.geometry.attributes.position.array;
    S.links.forEach((l,i)=>{const a=i*6;pos[a]=l.source.x||0;pos[a+1]=l.source.y||0;pos[a+2]=l.source.z||0;pos[a+3]=l.target.x||0;pos[a+4]=l.target.y||0;pos[a+5]=l.target.z||0;});lines.geometry.attributes.position.needsUpdate=true;lines.geometry.computeBoundingSphere();invalidate();
  }
  function updateCamera(){camera.position.set(target.x+radius*Math.sin(phi)*Math.sin(theta),target.y+radius*Math.cos(phi),target.z+radius*Math.sin(phi)*Math.cos(theta));camera.lookAt(target);camera.updateMatrixWorld();}
  function draw(){
    updateCamera();
    if(mesh&&colorsDirty){S.nodes.forEach((n,i)=>{color.set(nodeColor(n));if(isDim(n))color.multiplyScalar(.12);if(S.newSet.has(n.id))color.lerp(newColor,.55);else if(S.touchedSet.has(n.id))color.lerp(touchedColor,.55);if(S.selected===n)color.set(COL.accent);mesh.setColorAt(i,color);});if(mesh.instanceColor)mesh.instanceColor.needsUpdate=true;
      const arr=lines.geometry.attributes.color.array;S.links.forEach((l,i)=>{const selected=S.selected&&(l.source===S.selected||l.target===S.selected);color.set(l.isNew?COL.new:l.layer==='semantic'||l.relation==='calls'?COL.accent:'#777780');if((isCode()&&radius>500&&!selected)||(isDim(l.source)&&isDim(l.target)))color.multiplyScalar(.05);const a=i*6;arr[a]=arr[a+3]=color.r;arr[a+1]=arr[a+4]=color.g;arr[a+2]=arr[a+5]=color.b;});lines.geometry.attributes.color.needsUpdate=true;colorsDirty=false;
    }
    renderer.render(scene,camera);ctx.clearRect(0,0,labels.width,labels.height);
    const candidates=S.nodes.filter(n=>!isDim(n)&&(n===S.selected||n===S.hovered||S.term||radius<400||n.degree>5)).sort((a,b)=>(b===S.selected)-(a===S.selected)||(b.degree||0)-(a.degree||0)).slice(0,80);
    ctx.font='12px system-ui';ctx.fillStyle='#d6d6dc';ctx.textAlign='center';
    for(const n of candidates){projected.set(n.x||0,n.y||0,n.z||0).project(camera);if(projected.z>=-1&&projected.z<1&&Math.abs(projected.x)<1&&Math.abs(projected.y)<1)ctx.fillText(n.label,(projected.x+1)*labels.width/2,(1-projected.y)*labels.height/2+14);}
    if(S.selected||S.alwaysEdgeLabels){let count=0;ctx.fillStyle='#a99bd7';for(const l of S.links){if(count>=40)break;if(!S.alwaysEdgeLabels&&l.source!==S.selected&&l.target!==S.selected)continue;if(l.relation==='defines')continue;projected.set(((l.source.x||0)+(l.target.x||0))/2,((l.source.y||0)+(l.target.y||0))/2,((l.source.z||0)+(l.target.z||0))/2).project(camera);if(projected.z>=-1&&projected.z<1&&Math.abs(projected.x)<1&&Math.abs(projected.y)<1){ctx.fillText(l.relation,(projected.x+1)*labels.width/2,(1-projected.y)*labels.height/2);count++;}}}
  }
  // The page owns and mutates S directly, so check only cheap scalar/reference
  // state each frame; GPU uploads, projections and rendering occur only on change.
  function tick(){
    const state=[S.selected,S.hovered,S.term,S.filterNew,S.filterTouched,S.projectFilter,S.alwaysEdgeLabels,S.newSet,S.touchedSet,S.ns];
    if(state.some((v,i)=>v!==lastState[i])){const onlyHover=lastState.length&&state.every((v,i)=>i===1||v===lastState[i]);invalidate(!onlyHover);lastState=state;}
    if(dirty){dirty=false;draw();}requestAnimationFrame(tick);
  }
  function pick(e){updateCamera();const rect=canvas.getBoundingClientRect(),x=(e.clientX-rect.left)/rect.width*2-1,y=1-(e.clientY-rect.top)/rect.height*2;let best=null,dist=400;for(const n of S.nodes){if(isDim(n))continue;projected.set(n.x||0,n.y||0,n.z||0).project(camera);if(projected.z>=1||projected.z< -1)continue;const dx=(projected.x-x)*rect.width/2,dy=(projected.y-y)*rect.height/2,d=dx*dx+dy*dy;if(d<dist){dist=d;best=n;}}return best;}
  function distance(){const p=[...pointers.values()];return Math.hypot(p[0].x-p[1].x,p[0].y-p[1].y);}
  canvas.style.touchAction='none';
  canvas.addEventListener('pointerdown',e=>{if(e.pointerType==='mouse'&&e.button!==0)return;canvas.setPointerCapture(e.pointerId);pointers.set(e.pointerId,{x:e.clientX,y:e.clientY,startX:e.clientX,startY:e.clientY});if(pointers.size===1){gesture={moved:false};suppressClick=false;}else{gesture=null;suppressClick=true;} });
  canvas.addEventListener('pointermove',e=>{const p=pointers.get(e.pointerId);if(!p){if(!pointers.size&&e.pointerType!=='touch')S.hovered=pick(e);return;}const oldDistance=pointers.size===2?distance():0,dx=e.clientX-p.x,dy=e.clientY-p.y;p.x=e.clientX;p.y=e.clientY;
    if(pointers.size===2){const next=distance();if(oldDistance>0&&next>0){radius=Math.max(30,Math.min(50000,radius*oldDistance/next));S._userMoved=true;invalidate(true);}return;}
    if(pointers.size===1&&gesture){if(Math.hypot(p.x-p.startX,p.y-p.startY)>4)gesture.moved=true;if(gesture.moved){theta-=dx*.006;phi=Math.max(.05,Math.min(Math.PI-.05,phi+dy*.006));S._userMoved=true;invalidate();}}
  });
  function endPointer(e,cancelled){const p=pointers.get(e.pointerId);if(!p)return;if(!cancelled&&pointers.size===1&&gesture&&!gesture.moved&&!suppressClick)select(pick(e));pointers.delete(e.pointerId);if(pointers.size===1){const remaining=pointers.values().next().value;remaining.startX=remaining.x;remaining.startY=remaining.y;gesture={moved:false};suppressClick=true;}else if(!pointers.size)gesture=null;}
  canvas.addEventListener('pointerup',e=>endPointer(e,false));canvas.addEventListener('pointercancel',e=>endPointer(e,true));canvas.addEventListener('lostpointercapture',e=>endPointer(e,true));canvas.addEventListener('pointerleave',()=>{if(!pointers.size)S.hovered=null;});
  canvas.addEventListener('wheel',e=>{e.preventDefault();S._userMoved=true;radius=Math.max(30,Math.min(50000,radius*Math.exp(e.deltaY*.001)));invalidate(true);},{passive:false});
  resize();rebuild();Graph=api;tick();
}
