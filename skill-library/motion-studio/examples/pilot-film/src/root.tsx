import React from 'react';
import { Composition } from 'remotion';
import { Film } from './Film';
export const FilmRoot: React.FC = () => <Composition id="ContextualRetrieval" component={Film} width={1920} height={1080} fps={30} durationInFrames={1200} />;
