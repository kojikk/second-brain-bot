/* Bounded deterministic 3D layout: sampled repulsion and edge springs. */
onmessage = ({data}) => {
  const {nodes, edges, generation} = data, n = nodes.length;
  const spread = Number.isFinite(data.spread) && data.spread > 0 ? data.spread : 1;
  const validEdges = edges.filter(([i,j]) => Number.isInteger(i) && Number.isInteger(j) && i >= 0 && j >= 0 && i < n && j < n && i !== j);
  const p = new Float32Array(n * 3), v = new Float32Array(n * 3);
  const groups = new Map();
  for (let i=0;i<n;i++) {
    const key=nodes[i].community || 0;
    if (!groups.has(key)) groups.set(key, groups.size);
    const g=groups.get(key), a=i*2.399963, r=18*Math.cbrt(i+1);
    p[i*3]=Math.cos(a)*r + Math.cos(g*2.4)*100;
    p[i*3+1]=Math.sin(a)*r + Math.sin(g*2.4)*100;
    p[i*3+2]=((i*73)%101-50)*2;
  }
  const iterations=n>5000?35:80, samples=n>5000?8:16;
  for(let tick=0;tick<iterations;tick++) {
    const cool=1-tick/iterations;
    for(let i=0;i<n;i++) {
      let x=0,y=0,z=0, a=i*3;
      for(let k=0;k<Math.min(samples,n-1);k++) {
        const j=(i+1+((k*7919+tick*104729)%(n-1)))%n, b=j*3;
        let dx=p[a]-p[b],dy=p[a+1]-p[b+1],dz=p[a+2]-p[b+2];
        const d2=dx*dx+dy*dy+dz*dz+100, f=1200*spread/d2;
        x+=dx*f;y+=dy*f;z+=dz*f;
      }
      v[a]=(v[a]+x-p[a]*0.002)*0.75;
      v[a+1]=(v[a+1]+y-p[a+1]*0.002)*0.75;
      v[a+2]=(v[a+2]+z-p[a+2]*0.002)*0.75;
    }
    for(const [i,j] of validEdges) {
      const a=i*3,b=j*3,dx=p[b]-p[a],dy=p[b+1]-p[a+1],dz=p[b+2]-p[a+2];
      const d=Math.sqrt(dx*dx+dy*dy+dz*dz)||1, f=(d-50*spread)*0.002;
      v[a]+=dx*f;v[a+1]+=dy*f;v[a+2]+=dz*f;
      v[b]-=dx*f;v[b+1]-=dy*f;v[b+2]-=dz*f;
    }
    for(let i=0;i<p.length;i++) p[i]+=Math.max(-12,Math.min(12,v[i]))*cool;
  }
  postMessage({generation, positions:p},[p.buffer]);
};
