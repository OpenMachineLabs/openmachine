import React from 'react';
import {Composition, registerRoot} from 'remotion';
import {MotionFilm} from './film.jsx';
import {validateSpec, videoConfig} from './model.mjs';

const Root = () => <Composition
  id="ProjectToMotion"
  component={MotionFilm}
  width={1920}
  height={1080}
  fps={30}
  durationInFrames={450}
  defaultProps={{spec: null}}
  calculateMetadata={({props}) => {
    validateSpec(props.spec);
    return {...videoConfig(props.spec), props};
  }}
/>;

registerRoot(Root);
