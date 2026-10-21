import React from 'react';
import { Composition } from 'remotion';
import { Film } from './Film';
import { DirectionB } from './DirectionB';

export const FilmRoot: React.FC = () => (
  <>
    <Composition id="ContextualRetrieval" component={Film} width={1920} height={1080} fps={30} durationInFrames={1200} />
    <Composition id="DirectionB" component={DirectionB} width={1920} height={1080} fps={30} durationInFrames={1} />
  </>
);
