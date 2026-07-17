import {
  AbsoluteFill,
  Easing,
  interpolate,
  Sequence,
  spring,
  useCurrentFrame,
  useVideoConfig,
} from 'remotion';
import type {ReactNode} from 'react';

const fontFamily = 'Inter, SF Pro Display, SF Pro Text, Helvetica Neue, sans-serif';
const ink = '#f5f7ff';
const muted = '#9ca6bc';
const violet = '#8f7cff';

const fade = (frame: number, length: number) =>
  interpolate(frame, [0, 18, length - 18, length], [0, 1, 1, 0], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
  });

const Base = ({children}: {children: ReactNode}) => (
  <AbsoluteFill
    style={{
      background:
        'radial-gradient(circle at 70% 18%, #29235b 0%, #101424 30%, #070910 68%)',
      color: ink,
      fontFamily,
      overflow: 'hidden',
    }}
  >
    <div
      style={{
        position: 'absolute',
        width: 900,
        height: 900,
        borderRadius: '50%',
        left: -500,
        bottom: -600,
        background: '#284e75',
        filter: 'blur(120px)',
        opacity: 0.35,
      }}
    />
    {children}
  </AbsoluteFill>
);

const Copy = ({eyebrow, title, body}: {eyebrow: string; title: ReactNode; body: string}) => (
  <div style={{width: 700}}>
    <div style={{fontSize: 22, fontWeight: 600, color: '#a99cff', letterSpacing: 4, textTransform: 'uppercase'}}>
      {eyebrow}
    </div>
    <div style={{fontSize: 82, lineHeight: 0.98, fontWeight: 600, letterSpacing: -5, marginTop: 28}}>{title}</div>
    <div style={{fontSize: 28, lineHeight: 1.45, color: muted, maxWidth: 620, marginTop: 30}}>{body}</div>
  </div>
);

const Intro = () => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const reveal = spring({frame, fps, config: {damping: 18, mass: 0.8}});
  return (
    <Base>
      <AbsoluteFill style={{alignItems: 'center', justifyContent: 'center', opacity: fade(frame, 105)}}>
        <div style={{fontSize: 28, color: muted, letterSpacing: 2}}>MEET</div>
        <div style={{fontSize: 150, fontWeight: 600, letterSpacing: -10, transform: `scale(${0.86 + reveal * 0.14})`}}>Dispatch.</div>
        <div style={{fontSize: 34, color: '#b8c2d9', marginTop: 20}}>Impala work. Beautifully orchestrated.</div>
      </AbsoluteFill>
    </Base>
  );
};

const Window = ({frame}: {frame: number}) => {
  const lift = interpolate(frame, [0, 28], [80, 0], {easing: Easing.out(Easing.cubic), extrapolateRight: 'clamp'});
  const rows = [
    ['Revenue refresh', 'Running', '8m'],
    ['Customer health', 'Complete', '22s'],
    ['Daily inventory', 'Queued', '—'],
  ];
  return (
    <div style={{width: 900, height: 610, border: '1px solid #48516a', borderRadius: 30, background: 'rgba(14,18,31,.9)', boxShadow: '0 45px 120px #000a', transform: `translateY(${lift}px) rotateY(-5deg)`, padding: 28}}>
      <div style={{display: 'flex', gap: 9, marginBottom: 34}}>{['#ff6259', '#ffc04b', '#2acb67'].map((c) => <div key={c} style={{width: 13, height: 13, borderRadius: 10, background: c}} />)}</div>
      <div style={{display: 'flex', justifyContent: 'space-between', alignItems: 'center'}}>
        <div><b style={{fontSize: 28}}>Jobs</b><div style={{color: muted, marginTop: 8}}>Your workspace, in motion.</div></div>
        <div style={{padding: '14px 22px', borderRadius: 14, background: violet, fontWeight: 600}}>＋ New job</div>
      </div>
      <div style={{marginTop: 32}}>
        {rows.map(([name, status, time], i) => {
          const enter = spring({frame: frame - 10 - i * 7, fps: 30, config: {damping: 20}});
          return <div key={name} style={{display: 'grid', gridTemplateColumns: '1fr 180px 70px', alignItems: 'center', height: 105, borderTop: '1px solid #293047', opacity: enter, transform: `translateX(${(1 - enter) * 50}px)`}}>
            <span style={{fontSize: 22}}>{name}</span><span style={{color: status === 'Running' ? '#b7aaff' : status === 'Complete' ? '#6ee7ad' : muted}}>● &nbsp;{status}</span><span style={{color: muted}}>{time}</span>
          </div>;
        })}
      </div>
    </div>
  );
};

const Workspace = () => {
  const frame = useCurrentFrame();
  return <Base><AbsoluteFill style={{flexDirection: 'row', alignItems: 'center', gap: 130, padding: '0 120px', opacity: fade(frame, 120)}}><Copy eyebrow="One command center" title={<>Launch.<br/>Watch.<br/>Know.</>} body="Start Impala jobs, follow every step, and stay in control—without leaving your terminal."/><Window frame={frame}/></AbsoluteFill></Base>;
};

const Flow = () => {
  const frame = useCurrentFrame();
  const items = [['01', 'Choose SQL'], ['02', 'Set destination'], ['03', 'Dispatch']];
  return <Base><AbsoluteFill style={{justifyContent: 'center', padding: '0 150px', opacity: fade(frame, 105)}}><Copy eyebrow="Deliberately simple" title={<>From query<br/>to done.</>} body="A focused workflow turns complex infrastructure into three calm decisions."/><div style={{display: 'flex', gap: 22, marginTop: 70}}>{items.map(([n, label], i) => {const p = spring({frame: frame - i * 9, fps: 30, config: {damping: 16}}); return <div key={n} style={{width: 470, height: 150, borderRadius: 24, border: '1px solid #38415a', background: '#121725cc', padding: 28, transform: `translateY(${(1-p)*45}px)`, opacity: p}}><div style={{color: '#9085cf'}}>{n}</div><div style={{fontSize: 30, marginTop: 30}}>{label}</div></div>})}</div></AbsoluteFill></Base>;
};

const Finale = () => {
  const frame = useCurrentFrame();
  const glow = interpolate(frame, [0, 55], [0.25, 0.7], {extrapolateRight: 'clamp'});
  return <Base><AbsoluteFill style={{alignItems: 'center', justifyContent: 'center', opacity: fade(frame, 120)}}><div style={{position: 'absolute', width: 520, height: 520, borderRadius: '50%', background: violet, filter: 'blur(150px)', opacity: glow}}/><div style={{fontSize: 118, fontWeight: 600, letterSpacing: -8, zIndex: 1}}>Power, without the noise.</div><div style={{fontSize: 34, color: '#c4cbe0', marginTop: 28, zIndex: 1}}>Dispatch for Impala</div><div style={{fontSize: 20, color: '#7f89a1', marginTop: 75, zIndex: 1}}>SERVER-SIDE · SECURE · SUPERVISED</div></AbsoluteFill></Base>;
};

export const DispatchFilm = () => (
  <AbsoluteFill>
    <Sequence durationInFrames={105}><Intro /></Sequence>
    <Sequence from={105} durationInFrames={120}><Workspace /></Sequence>
    <Sequence from={225} durationInFrames={105}><Flow /></Sequence>
    <Sequence from={330} durationInFrames={120}><Finale /></Sequence>
  </AbsoluteFill>
);
