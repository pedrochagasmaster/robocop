import {Composition} from 'remotion';
import {DispatchFilm} from './video';

export const Root = () => (
  <Composition
    id="DispatchFilm"
    component={DispatchFilm}
    durationInFrames={450}
    fps={30}
    width={1920}
    height={1080}
  />
);

